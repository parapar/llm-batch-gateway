"""Batch expiry and result-file cleanup (M5's "expiry/limits" item).

Two independent jobs, both idempotent and safe to run repeatedly:

1. expire_overdue_batches: a batch that's been sitting in
   validating/in_progress past its expires_at (set at submission from
   completion_window -- see batch_ops.create_batch) is stopped the same
   way cancel_batch stops one, releasing whatever's left of its
   reservation, except the terminal status is EXPIRED instead of
   CANCELLED and it's the system doing it, not the student.
2. purge_expired_results: once a *finished* batch (completed, failed,
   cancelled, or expired) is older than result_retention_days past
   whichever terminal timestamp applies, its output/error blobs and
   FileObject rows are deleted from disk and the DB (the input file is
   deliberately left alone -- it's what "result_retention_days" means,
   and Batch.input_file_id isn't nullable, so purging it would either
   need a schema change or leave a dangling FK; the input JSONL a
   student uploaded is also tiny compared to a run's output). This is
   the only destructive, unrecoverable step in the whole service --
   once a batch's results are purged, GET /v1/files/{id} on its
   output/error file 404s exactly like it never existed.

Both are driven by RetentionJob.run_forever(), started unconditionally
from main.py's lifespan (unlike the M3 dispatcher, this doesn't depend
on any nodes being configured -- expiry and cleanup make sense for an
API-only deployment too).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from batchsvc import batch_ops
from batchsvc.config import Settings
from batchsvc.db import Database
from batchsvc.models import Batch, BatchStatus, FileObject, User

logger = logging.getLogger("batchsvc.retention")

_TERMINAL_TIMESTAMP_FIELDS: dict[BatchStatus, str] = {
    BatchStatus.COMPLETED: "completed_at",
    BatchStatus.FAILED: "failed_at",
    BatchStatus.CANCELLED: "cancelled_at",
    BatchStatus.EXPIRED: "expired_at",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    """See eta._as_utc: SQLite doesn't round-trip tzinfo across a session
    boundary, so a value just read back from the DB may be naive even
    though everything this module writes is UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def expire_overdue_batches(db: Session) -> int:
    overdue = (
        db.query(Batch)
        .filter(
            Batch.status.in_([BatchStatus.VALIDATING, BatchStatus.IN_PROGRESS]),
            Batch.expires_at.is_not(None),
            Batch.expires_at < _now(),
        )
        .all()
    )
    for batch in overdue:
        user = db.get(User, batch.user_id)
        batch_ops.expire_batch(db, user=user, batch=batch)
        logger.info("expired overdue batch %s (user=%s)", batch.id, batch.user_id)
    return len(overdue)


def _delete_file(file_obj: FileObject | None) -> None:
    if file_obj is None:
        return
    path = Path(file_obj.path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete blob at %s for file %s", path, file_obj.id)


def purge_expired_results(db: Session, settings: Settings) -> int:
    cutoff_by_status = {
        status: _now() - timedelta(days=settings.result_retention_days)
        for status in _TERMINAL_TIMESTAMP_FIELDS
    }

    candidates = (
        db.query(Batch)
        .filter(Batch.status.in_(list(_TERMINAL_TIMESTAMP_FIELDS)), Batch.purged_at.is_(None))
        .all()
    )

    purged_count = 0
    for batch in candidates:
        timestamp_attr = _TERMINAL_TIMESTAMP_FIELDS[batch.status]
        terminal_at = getattr(batch, timestamp_attr)
        if terminal_at is None or _as_utc(terminal_at) > cutoff_by_status[batch.status]:
            continue

        output_file = db.get(FileObject, batch.output_file_id) if batch.output_file_id else None
        error_file = db.get(FileObject, batch.error_file_id) if batch.error_file_id else None

        # Null the FK references (and flush) before deleting the FileObject
        # rows below -- without an ORM relationship() between Batch and
        # FileObject, the unit of work won't infer that ordering on its
        # own, and SQLite's immediate FK checking rejects a DELETE that
        # runs while a batches row still points at it (same class of bug
        # as batch_ops._maybe_finalize_batch hit in the other direction).
        batch.output_file_id = None
        batch.error_file_id = None
        batch.purged_at = _now()
        db.flush()

        for file_obj in (output_file, error_file):
            if file_obj is None:
                continue
            _delete_file(file_obj)
            db.delete(file_obj)

        purged_count += 1
        logger.info("purged result files for batch %s (status=%s)", batch.id, batch.status)

    if purged_count:
        db.commit()
    return purged_count


class RetentionJob:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def run_once(self) -> tuple[int, int]:
        with self.db.session_scope() as session:
            expired = expire_overdue_batches(session)
        with self.db.session_scope() as session:
            purged = purge_expired_results(session, self.settings)
        return expired, purged

    async def run_forever(self) -> None:
        while True:
            try:
                # run_once() is pure sync DB work; run it off the event
                # loop so a lock wait (expire_batch/release both go through
                # ledger.py's threading.Lock, same as the dispatcher's
                # settlement path -- see dispatcher._execute_task) can't
                # stall every other request this process is serving.
                expired, purged = await asyncio.to_thread(self.run_once)
                if expired or purged:
                    logger.info("retention pass: expired=%d purged=%d", expired, purged)
            except Exception:
                logger.exception("retention pass failed")
            await asyncio.sleep(self.settings.retention_check_interval_seconds)
