"""The dispatcher: claims pending tasks, load-balances them across
healthy llama-server nodes, and drives each one to completion via
batch_ops.complete_task/fail_task.

Capacity is tracked as an in-process in-flight counter per node (never
trusting a node's own /slots), per docs/PLAN.md. Only this class
mutates it, and only ever inside the event loop it runs on -- no locking
needed for that dict, unlike ledger.py's cross-request budget lock.

Claiming (fair-share round-robin across users, then FIFO by batch, then
line order) and per-task execution (with retry/backoff and a fresh DB
session per attempt) are separate methods precisely so tests can drive
them deterministically (`dispatch_once()` does one claim-and-run pass)
rather than depend on the real `run_forever()` polling loop, matching
how M2's tests exercise complete_task/fail_task directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from batchsvc import batch_ops
from batchsvc.config import Settings
from batchsvc.db import Database
from batchsvc.llama_client import LlamaClient, NodeRequestError
from batchsvc.models import Batch, BatchStatus, Node, NodeHealth, Task, TaskStatus

logger = logging.getLogger("batchsvc.dispatcher")

ClientFactory = Callable[[str], LlamaClient]


def _now() -> datetime:
    return datetime.now(UTC)


def _default_client_factory(settings: Settings) -> ClientFactory:
    def factory(base_url: str) -> LlamaClient:
        return LlamaClient(base_url, timeout=settings.task_request_timeout_seconds)

    return factory


def _claim_tasks(db: Session, limit: int) -> list[Task]:
    """Picks up to `limit` PENDING tasks belonging to in_progress batches,
    round-robining one task per user per pass so a single large batch
    can't starve everyone else's work, and preserving each user's own
    FIFO (batch submission order, then line order) within their share."""
    if limit <= 0:
        return []

    rows = (
        db.query(Task, Batch.user_id)
        .join(Batch, Task.batch_id == Batch.id)
        .filter(Task.status == TaskStatus.PENDING, Batch.status == BatchStatus.IN_PROGRESS)
        .order_by(Batch.created_at, Task.line_index)
        .all()
    )
    if not rows:
        return []

    by_user: dict[str, deque[Task]] = defaultdict(deque)
    user_order: list[str] = []
    for task, user_id in rows:
        if user_id not in by_user:
            user_order.append(user_id)
        by_user[user_id].append(task)

    claimed: list[Task] = []
    while len(claimed) < limit and any(by_user[u] for u in user_order):
        for u in user_order:
            if len(claimed) >= limit:
                break
            if by_user[u]:
                claimed.append(by_user[u].popleft())
    return claimed


class Dispatcher:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
    ):
        self.db = db
        self.settings = settings
        self._client_factory = client_factory or _default_client_factory(settings)
        self._clients: dict[str, LlamaClient] = {}
        self._in_flight: dict[str, int] = {}

    def _client_for(self, base_url: str) -> LlamaClient:
        client = self._clients.get(base_url)
        if client is None:
            client = self._client_factory(base_url)
            self._clients[base_url] = client
        return client

    # --- Setup / recovery, run once at process start. ---

    def sync_nodes_from_config(self) -> None:
        """Upserts Node rows from settings.nodes. A node removed from
        config is marked DISABLED (not deleted -- tasks.node_name is a
        plain string, not a FK, so there's nothing to cascade, and
        keeping the row preserves its throughput history)."""
        configured = {n.name: n for n in self.settings.nodes}
        with self.db.session_scope() as session:
            existing = {n.name: n for n in session.query(Node).all()}
            for name, cfg in configured.items():
                row = existing.get(name)
                if row is None:
                    session.add(
                        Node(name=name, base_url=cfg.base_url, parallel_slots=cfg.parallel_slots)
                    )
                else:
                    row.base_url = cfg.base_url
                    row.parallel_slots = cfg.parallel_slots
                    if row.health == NodeHealth.DISABLED:
                        row.health = NodeHealth.HEALTHY
                        row.consecutive_failures = 0
            for name, row in existing.items():
                if name not in configured and row.health != NodeHealth.DISABLED:
                    row.health = NodeHealth.DISABLED
            session.commit()

    def recover_running_tasks(self) -> int:
        """Any task still RUNNING at process start belonged to a
        dispatcher that no longer exists (a crash, or this is a fresh
        process) -- nothing is actually in flight for it, so it goes
        back to PENDING to be claimed again. Returns the count reset."""
        with self.db.session_scope() as session:
            stuck = session.query(Task).filter(Task.status == TaskStatus.RUNNING).all()
            for task in stuck:
                task.status = TaskStatus.PENDING
                task.node_name = None
                task.started_at = None
            session.commit()
            return len(stuck)

    def startup(self) -> None:
        self.sync_nodes_from_config()
        recovered = self.recover_running_tasks()
        if recovered:
            logger.info("recovered %d task(s) stuck in RUNNING from a previous process", recovered)

    # --- Health. ---

    async def health_check_all(self) -> None:
        with self.db.session_scope() as session:
            nodes = (
                session.query(Node).filter(Node.health != NodeHealth.DISABLED).all()
            )
            for node in nodes:
                client = self._client_for(node.base_url)
                ok = await client.health()
                if ok:
                    node.consecutive_failures = 0
                    node.health = NodeHealth.HEALTHY
                else:
                    node.consecutive_failures += 1
                    if node.consecutive_failures >= self.settings.node_unhealthy_threshold:
                        node.health = NodeHealth.UNHEALTHY
            session.commit()

    # --- Dispatch. ---

    async def dispatch_once(self) -> int:
        """One claim-and-run pass: claims as many PENDING tasks as there
        is free capacity across healthy nodes, dispatches them all
        concurrently, and returns once every one of them has settled
        (completed or failed -- including retries). Returns how many
        tasks were claimed this pass (0 means nothing to do right now)."""
        with self.db.session_scope() as session:
            healthy_nodes = session.query(Node).filter(Node.health == NodeHealth.HEALTHY).all()
            if not healthy_nodes:
                return 0

            capacity = {n.name: n.parallel_slots - self._in_flight.get(n.name, 0) for n in healthy_nodes}
            total_capacity = sum(c for c in capacity.values() if c > 0)
            if total_capacity <= 0:
                return 0

            claimed = _claim_tasks(session, total_capacity)
            if not claimed:
                return 0

            assignments: list[tuple[str, str, str]] = []  # (task_id, node_name, base_url)
            local_capacity = dict(capacity)
            node_by_name = {n.name: n for n in healthy_nodes}
            now_ts = _now()
            for task in claimed:
                node_name = max(local_capacity, key=lambda name: local_capacity[name])
                if local_capacity[node_name] <= 0:
                    break  # shouldn't happen (claimed <= total_capacity), but don't over-assign
                local_capacity[node_name] -= 1
                task.status = TaskStatus.RUNNING
                task.node_name = node_name
                task.started_at = now_ts
                task.attempts += 1
                assignments.append((task.id, node_name, node_by_name[node_name].base_url))
            session.commit()

        for _task_id, node_name, _base_url in assignments:
            self._in_flight[node_name] = self._in_flight.get(node_name, 0) + 1

        await asyncio.gather(*(self._execute_task(*a) for a in assignments))
        return len(assignments)

    async def _execute_task(self, task_id: str, node_name: str, base_url: str) -> None:
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            body = dict(task.request_body)

        client = self._client_for(base_url)
        result: dict | None = None
        last_error = "unknown error"
        max_attempts = max(1, self.settings.task_max_attempts)
        for attempt in range(max_attempts):
            try:
                result = await client.chat_completion(body)
                break
            except NodeRequestError as e:
                last_error = str(e)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2**attempt)

        self._in_flight[node_name] = max(0, self._in_flight.get(node_name, 0) - 1)

        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if result is not None:
                usage = result.get("usage") or {}
                batch_ops.complete_task(
                    session,
                    task=task,
                    response_body=result,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    blob_dir=self.settings.blob_dir,
                    eta_alpha=self.settings.eta_ewma_alpha,
                )
            else:
                batch_ops.fail_task(
                    session,
                    task=task,
                    error=f"failed after {max_attempts} attempt(s): {last_error}",
                    blob_dir=self.settings.blob_dir,
                )

    # --- Background loop for production use (main.py's lifespan). ---

    async def run_forever(self) -> None:
        last_health_check = 0.0
        loop = asyncio.get_event_loop()
        while True:
            now = loop.time()
            if now - last_health_check >= self.settings.health_check_interval_seconds:
                await self.health_check_all()
                last_health_check = now
            dispatched = await self.dispatch_once()
            if dispatched == 0:
                await asyncio.sleep(self.settings.dispatch_idle_poll_seconds)

    async def aclose(self) -> None:
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
