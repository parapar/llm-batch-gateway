#!/usr/bin/env python3
"""End-to-end load test against real subprocess servers.

Spins up N stub llama-server nodes (scripts/stub_llama_node.py, real
HTTP over real sockets, with configurable artificial latency) and a
real batchsvc server pointed at them, then floods it with concurrent
"students" each submitting a batch, polling status, and downloading
results -- the same path a real class of students would exercise, just
compressed into one process's worth of asyncio tasks.

Not part of the pytest suite: it's slow by design (real subprocesses,
real sleeps for latency) and prints a human-readable report rather than
asserting pass/fail. Run it manually as a sanity check before/after a
change to the dispatcher, or point --node-url at a real llama-server to
get a feel for actual throughput on real hardware instead of the stub.

Usage:
    python scripts/load_test.py
    python scripts/load_test.py --students 20 --lines-per-batch 15 --stub-nodes 2 --slots-per-node 4
    python scripts/load_test.py --node-url http://halo-1.local:8080 --node-url http://halo-2.local:8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class BatchResult:
    student: str
    submitted_at: float
    completed_at: float | None = None
    status: str = "unknown"
    request_counts: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def duration(self) -> float | None:
        if self.completed_at is None:
            return None
        return self.completed_at - self.submitted_at


async def _wait_for_health(client: httpx.AsyncClient, url: str, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = await client.get(url, timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_error = e
        await asyncio.sleep(0.2)
    raise RuntimeError(f"{url} did not become healthy in time (last error: {last_error})")


class SubprocessServer:
    def __init__(self, name: str, args: list[str], *, env: dict[str, str], cwd: Path):
        self.name = name
        self.process = subprocess.Popen(  # noqa: S603
            args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def tail_output(self) -> str:
        if self.process.stdout is None:
            return ""
        return self.process.stdout.read() or ""


def _jsonl_line(custom_id: str, content: str, max_tokens: int) -> str:
    return json.dumps(
        {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens},
        }
    )


async def _run_student(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    admin_headers: dict,
    student_index: int,
    lines_per_batch: int,
    poll_interval: float,
    timeout: float,
) -> BatchResult:
    username = f"loadtest-student-{student_index}"
    user_resp = await client.post(
        f"{base_url}/admin/users", json={"username": username}, headers=admin_headers
    )
    user = user_resp.json()
    key_resp = await client.post(
        f"{base_url}/admin/users/{user['id']}/api-keys", json={}, headers=admin_headers
    )
    api_key = key_resp.json()["key"]
    await client.post(
        f"{base_url}/admin/users/{user['id']}/budget/grant",
        json={"tokens": 1_000_000},
        headers=admin_headers,
    )
    auth = {"Authorization": f"Bearer {api_key}"}

    lines = "\n".join(
        _jsonl_line(f"req-{i}", f"Question {i} from {username}: what is {i} squared?", 80)
        for i in range(lines_per_batch)
    )
    file_resp = await client.post(
        f"{base_url}/v1/files",
        files={"file": ("input.jsonl", (lines + "\n").encode(), "application/jsonl")},
        data={"purpose": "batch"},
        headers=auth,
    )
    file_id = file_resp.json()["id"]

    submitted_at = time.monotonic()
    batch_resp = await client.post(
        f"{base_url}/v1/batches",
        json={"input_file_id": file_id, "endpoint": "/v1/chat/completions", "completion_window": "24h"},
        headers=auth,
    )
    if batch_resp.status_code != 201:
        return BatchResult(
            student=username, submitted_at=submitted_at, error=f"submit failed: {batch_resp.text}"
        )
    batch = batch_resp.json()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status_resp = await client.get(f"{base_url}/v1/batches/{batch['id']}", headers=auth)
            status = status_resp.json()
        except httpx.HTTPError:
            # A transient poll hiccup (client-side timeout, connection reset)
            # isn't a load-test failure -- just try again next tick, same as
            # any real polling client would.
            await asyncio.sleep(poll_interval)
            continue
        if status["status"] in ("completed", "failed", "cancelled", "expired"):
            return BatchResult(
                student=username,
                submitted_at=submitted_at,
                completed_at=time.monotonic(),
                status=status["status"],
                request_counts=status["request_counts"],
            )
        await asyncio.sleep(poll_interval)

    return BatchResult(
        student=username, submitted_at=submitted_at, status="timed_out", error="did not finish in time"
    )


def _print_report(results: list[BatchResult], wall_clock: float) -> None:
    completed = [r for r in results if r.status == "completed"]
    failed = [r for r in results if r.status != "completed"]
    total_requests = sum(r.request_counts.get("total", 0) for r in results)
    total_task_completed = sum(r.request_counts.get("completed", 0) for r in results)
    total_task_failed = sum(r.request_counts.get("failed", 0) for r in results)

    print("\n" + "=" * 60)
    print("LOAD TEST REPORT")
    print("=" * 60)
    print(f"students (batches):     {len(results)}")
    print(f"  completed:             {len(completed)}")
    print(f"  failed/timed out:      {len(failed)}")
    print(f"total requests:          {total_requests}")
    print(f"  task-level completed:  {total_task_completed}")
    print(f"  task-level failed:     {total_task_failed}")
    print(f"wall clock:              {wall_clock:.1f}s")
    if total_requests:
        print(f"throughput:              {total_requests / wall_clock:.2f} requests/sec")

    durations = [r.duration for r in completed if r.duration is not None]
    if durations:
        durations.sort()
        p50 = statistics.median(durations)
        p95 = durations[int(len(durations) * 0.95) - 1] if len(durations) > 1 else durations[0]
        print(f"batch completion time:  p50={p50:.1f}s  p95={p95:.1f}s  max={max(durations):.1f}s")

    if failed:
        print("\nfailures:")
        for r in failed:
            print(f"  {r.student}: status={r.status} error={r.error}")
    print("=" * 60)


async def _drive_load_test(args: argparse.Namespace, base_url: str) -> list[BatchResult]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        await _wait_for_health(client, f"{base_url}/healthz")
        admin_headers = {"Authorization": f"Bearer {args.admin_token}"}

        start = time.monotonic()
        results = await asyncio.gather(
            *(
                _run_student(
                    client,
                    base_url=base_url,
                    admin_headers=admin_headers,
                    student_index=i,
                    lines_per_batch=args.lines_per_batch,
                    poll_interval=args.poll_interval,
                    timeout=args.timeout,
                )
                for i in range(args.students)
            )
        )
        wall_clock = time.monotonic() - start
    _print_report(results, wall_clock)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--students", type=int, default=5, help="concurrent students, each submitting one batch"
    )
    parser.add_argument("--lines-per-batch", type=int, default=10)
    parser.add_argument("--stub-nodes", type=int, default=2, help="number of built-in stub nodes to spin up")
    parser.add_argument("--slots-per-node", type=int, default=4)
    parser.add_argument("--stub-latency", type=float, default=0.3, help="simulated seconds per stub request")
    parser.add_argument(
        "--stub-failure-rate",
        type=float,
        default=0.0,
        help="0.0-1.0: fraction of stub requests that fail transiently (exercises dispatcher retries)",
    )
    parser.add_argument(
        "--node-url", action="append", default=[], help="use a real node instead of a stub (repeatable)"
    )
    parser.add_argument("--port", type=int, default=8199, help="port for the batchsvc server under test")
    parser.add_argument("--base-stub-port", type=int, default=9100)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=120.0, help="max seconds to wait per batch")
    parser.add_argument("--keep-tmp", action="store_true", help="don't delete the temp config/data dir")
    args = parser.parse_args()

    args.admin_token = secrets.token_hex(16)
    tmp_dir = Path(tempfile.mkdtemp(prefix="batchsvc-loadtest-"))
    print(f"working dir: {tmp_dir}")

    servers: list[SubprocessServer] = []
    exit_code = 0
    try:
        node_urls = list(args.node_url)
        if not node_urls:
            for i in range(args.stub_nodes):
                port = args.base_stub_port + i
                env = dict(os.environ)
                env["STUB_LATENCY_SECONDS"] = str(args.stub_latency)
                env["STUB_FAILURE_RATE"] = str(args.stub_failure_rate)
                stub = SubprocessServer(
                    f"stub-{i}",
                    [sys.executable, "-m", "uvicorn", "scripts.stub_llama_node:app", "--port", str(port)],
                    env=env,
                    cwd=REPO_ROOT,
                )
                servers.append(stub)
                node_urls.append(f"http://127.0.0.1:{port}")

        config = {
            "database_path": str(tmp_dir / "db.sqlite"),
            "blob_dir": str(tmp_dir / "blobs"),
            "admin_token": args.admin_token,
            "health_check_interval_seconds": 2.0,
            "dispatch_idle_poll_seconds": 0.1,
            "nodes": [
                {"name": f"node-{i}", "base_url": url, "parallel_slots": args.slots_per_node}
                for i, url in enumerate(node_urls)
            ],
        }
        config_path = tmp_dir / "config.yaml"
        config_path.write_text(yaml.safe_dump(config))

        server_env = dict(os.environ)
        server_env["BATCHSVC_CONFIG"] = str(config_path)
        api_server = SubprocessServer(
            "batchsvc",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "batchsvc.main:create_app",
                "--factory",
                "--port",
                str(args.port),
            ],
            env=server_env,
            cwd=REPO_ROOT,
        )
        servers.append(api_server)

        base_url = f"http://127.0.0.1:{args.port}"
        results = asyncio.run(_drive_load_test(args, base_url))
        if any(r.status != "completed" for r in results):
            exit_code = 1
    except Exception:
        exit_code = 2
        traceback.print_exc(file=sys.stderr)
    finally:
        # Stop every subprocess *before* touching its output: tail_output()
        # does a blocking read() on the pipe, which never returns EOF (i.e.
        # hangs forever) while the process is still alive.
        for s in servers:
            s.stop()
        if exit_code == 2:
            for s in servers:
                print(f"\n--- {s.name} output ---\n{s.tail_output()[-2000:]}", file=sys.stderr)
        if args.keep_tmp:
            print(f"kept working dir: {tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
