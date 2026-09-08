"""ETA estimation for the batch status endpoint.

Two pieces, matching docs/PLAN.md's "ETA model" section:

1. A rolling cluster-throughput estimate (update_dispatch_stats), fed by
   every task the dispatcher actually completes -- see
   batch_ops.complete_task. Tracked as a single global EWMA (DispatchStat,
   id="global") rather than per-node, since a deployment's nodes all
   serve the same model and are treated as one shared throughput pool.
2. A per-batch estimate (estimate_batch_eta) combining that throughput
   with the *expected* (not worst-case) remaining work: this batch's own
   pending/running tasks, plus a simple FIFO-by-submission-time
   approximation of the work "ahead" of it in other in_progress batches.

That FIFO approximation is a deliberate simplification: the dispatcher's
actual claiming order is fair-share round-robin across users (see
dispatcher._claim_tasks), not strict batch FIFO. Simulating the exact
round-robin here would need to replay it for every status request; FIFO
by batch creation time is close enough for an ETA (which is a rough
estimate by nature) and is what "queue_position" means below: how many
still-in-progress batches were submitted before this one, not this
batch's exact position in the round-robin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from batchsvc.config import Settings
from batchsvc.models import Batch, BatchStatus, DispatchStat, Node, NodeHealth, Task, TaskStatus

# Prefill (prompt) tokens are processed much faster than generation, so a
# pending task's "weighted" remaining work counts its prompt at a
# fraction of a generated token -- both for recording throughput samples
# and for estimating remaining work.
PROMPT_TOKEN_WEIGHT = 0.1


def _as_utc(dt: datetime) -> datetime:
    """SQLite has no native timezone storage: a DateTime(timezone=True)
    column round-trips as naive once it's been written and re-read in a
    later session, even though a value just set in Python (like
    finished_at, a few lines above this call) is still tz-aware. Every
    datetime this module writes is UTC (see _now() callers), so a naive
    value is always safe to treat as UTC rather than a mixed-awareness
    subtraction blowing up."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def update_dispatch_stats(db: Session, *, task: Task, alpha: float) -> None:
    """Feeds one completed task's real (node, duration, tokens) outcome
    into the rolling EWMAs. A no-op if the task was never actually
    dispatched (started_at unset) -- e.g. batch_ops.complete_task called
    directly in tests without going through the dispatcher, which
    shouldn't count as a throughput sample."""
    if task.started_at is None or task.finished_at is None:
        return
    duration_seconds = (_as_utc(task.finished_at) - _as_utc(task.started_at)).total_seconds()
    if duration_seconds <= 0:
        return

    weighted_tokens = (task.prompt_tokens or 0) * PROMPT_TOKEN_WEIGHT + (task.completion_tokens or 0)
    if weighted_tokens <= 0:
        return
    sample_tps = weighted_tokens / duration_seconds

    stat = db.get(DispatchStat, "global")
    if stat is None:
        stat = DispatchStat(id="global", sample_count=0)
        db.add(stat)

    stat.tokens_per_second_ewma = (
        sample_tps
        if stat.tokens_per_second_ewma is None
        else alpha * sample_tps + (1 - alpha) * stat.tokens_per_second_ewma
    )
    completion_sample = task.completion_tokens or 0
    if completion_sample > 0:
        stat.avg_completion_tokens_ewma = (
            float(completion_sample)
            if stat.avg_completion_tokens_ewma is None
            else alpha * completion_sample + (1 - alpha) * stat.avg_completion_tokens_ewma
        )
    stat.sample_count += 1


@dataclass
class BatchEta:
    estimated_seconds_remaining: float | None
    estimated_completion_at: datetime | None
    queue_position: int | None
    confidence: str  # "normal" | "low" | "unavailable"


_UNAVAILABLE = BatchEta(None, None, None, "unavailable")


def _remaining_weighted_tokens(db: Session, batch_id: str, *, avg_completion_tokens: float) -> float:
    pending = (
        db.query(Task)
        .filter(Task.batch_id == batch_id, Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        .all()
    )
    total = 0.0
    for t in pending:
        expected_completion = (
            min(t.max_tokens, avg_completion_tokens) if t.max_tokens else avg_completion_tokens
        )
        total += t.prompt_tokens_estimate * PROMPT_TOKEN_WEIGHT + expected_completion
    return total


def estimate_batch_eta(db: Session, settings: Settings, batch: Batch) -> BatchEta:
    if batch.status not in (BatchStatus.VALIDATING, BatchStatus.IN_PROGRESS):
        return _UNAVAILABLE  # terminal (or finalizing): nothing left to wait for
    if not settings.nodes:
        return _UNAVAILABLE  # no dispatcher running to ever complete this

    healthy_slots = sum(
        n.parallel_slots for n in db.query(Node).filter(Node.health == NodeHealth.HEALTHY).all()
    )
    if healthy_slots <= 0:
        return BatchEta(None, None, None, "unavailable")

    stat = db.get(DispatchStat, "global")
    sample_count = stat.sample_count if stat else 0
    confidence = "normal" if sample_count >= settings.eta_min_samples_for_confidence else "low"

    per_slot_tps = (stat.tokens_per_second_ewma if stat else None) or settings.eta_bootstrap_tokens_per_second
    avg_completion_tokens = (
        stat.avg_completion_tokens_ewma if stat else None
    ) or settings.eta_bootstrap_completion_tokens
    cluster_tps = per_slot_tps * healthy_slots
    if cluster_tps <= 0:
        return BatchEta(None, None, None, "unavailable")

    this_batch_remaining = _remaining_weighted_tokens(
        db, batch.id, avg_completion_tokens=avg_completion_tokens
    )

    ahead_batches = (
        db.query(Batch)
        .filter(Batch.status == BatchStatus.IN_PROGRESS, Batch.created_at < batch.created_at)
        .all()
    )
    queue_position = len(ahead_batches)
    ahead_weighted = sum(
        _remaining_weighted_tokens(db, b.id, avg_completion_tokens=avg_completion_tokens)
        for b in ahead_batches
    )

    seconds_remaining = (ahead_weighted + this_batch_remaining) / cluster_tps
    completion_at = datetime.now(UTC) + timedelta(seconds=seconds_remaining)
    return BatchEta(seconds_remaining, completion_at, queue_position, confidence)
