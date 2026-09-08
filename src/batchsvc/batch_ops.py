"""Service logic for the /v1/files and /v1/batches surface.

Two things live here rather than in the routers:

1. JSONL parsing/validation (parse_batch_input) and batch submission
   (create_batch) -- the M2 scope proper.
2. Task completion/finalization (complete_task, fail_task, cancel_batch)
   -- the sink end of the pipeline. The M3 dispatcher (batchsvc.dispatcher)
   calls complete_task/fail_task once per task it settles; a few tests
   also call them directly (bypassing a real dispatcher) to exercise the
   pipeline deterministically. Keeping this in one module means there's
   exactly one place that writes to Task/Batch rows outside of ledger.py's
   own ownership of Budget rows.

Atomicity note: ledger.reserve/release/charge each commit the session
themselves (see ledger.py). Every function below sets all the Batch/Task
fields it needs to *before* calling into ledger, so that one ledger call's
commit captures the whole state transition atomically -- if reserve()
raises InsufficientBudgetError, the caller's session teardown rolls back
the as-yet-uncommitted Batch/Task rows along with it (see deps.get_db).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from batchsvc import eta, ledger, tokens
from batchsvc.blobs import write_blob
from batchsvc.errors import ConflictError, InvalidRequestError, NotFoundError
from batchsvc.models import (
    Batch,
    BatchStatus,
    FileObject,
    FilePurpose,
    Task,
    TaskStatus,
    User,
)

SUPPORTED_ENDPOINT = "/v1/chat/completions"
SUPPORTED_COMPLETION_WINDOWS = {"24h"}

_CANCELLABLE_STATUSES = {BatchStatus.VALIDATING, BatchStatus.IN_PROGRESS}
_MAX_ERRORS_SHOWN = 5


def _now() -> datetime:
    return datetime.now(UTC)


def new_file_id() -> str:
    return f"file_{uuid.uuid4().hex}"


# --- Ownership lookups shared by the files and batches routers. ---


def get_owned_file_or_404(db: Session, user: User, file_id: str) -> FileObject:
    f = db.get(FileObject, file_id)
    if f is None or f.user_id != user.id:
        raise NotFoundError(f"No such file '{file_id}'.")
    return f


def get_owned_batch_or_404(db: Session, user: User, batch_id: str) -> Batch:
    b = db.get(Batch, batch_id)
    if b is None or b.user_id != user.id:
        raise NotFoundError(f"No such batch '{batch_id}'.")
    return b


# --- Input file parsing/validation. ---


@dataclass
class ParsedLine:
    line_index: int
    custom_id: str
    body: dict
    prompt_tokens: int
    max_tokens: int

    @property
    def reserved_tokens(self) -> int:
        return self.prompt_tokens + self.max_tokens


def parse_batch_input(raw: bytes, *, endpoint: str, default_max_tokens: int) -> list[ParsedLine]:
    """Parses and validates a batch input JSONL file: one JSON object per
    non-blank line, each shaped like OpenAI's batch input line ({custom_id,
    method, url, body}). Collects every error found (rather than stopping
    at the first) so a submitter sees the whole picture in one round trip;
    raises InvalidRequestError (400) if any line is invalid, and nothing
    about the batch is created."""
    text = raw.decode("utf-8", errors="replace")
    raw_lines = [line for line in text.split("\n") if line.strip()]
    if not raw_lines:
        raise InvalidRequestError("Input file has no request lines.", param="input_file_id")

    parsed: list[ParsedLine] = []
    errors: list[str] = []
    seen_custom_ids: set[str] = set()

    for idx, raw_line in enumerate(raw_lines):
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as e:
            errors.append(f"line {idx}: invalid JSON ({e.msg})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {idx}: must be a JSON object")
            continue

        custom_id = obj.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id:
            errors.append(f"line {idx}: missing or invalid 'custom_id'")
            continue
        if custom_id in seen_custom_ids:
            errors.append(f"line {idx}: duplicate custom_id '{custom_id}'")
            continue

        if obj.get("method") != "POST":
            errors.append(f"line {idx}: 'method' must be 'POST'")
            continue
        if obj.get("url") != endpoint:
            errors.append(f"line {idx}: 'url' must be '{endpoint}', got {obj.get('url')!r}")
            continue

        body = obj.get("body")
        if not isinstance(body, dict):
            errors.append(f"line {idx}: missing or invalid 'body'")
            continue
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            errors.append(f"line {idx}: body.messages must be a non-empty list")
            continue
        max_tokens = body.get("max_tokens")
        if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens <= 0):
            errors.append(f"line {idx}: body.max_tokens must be a positive integer")
            continue

        seen_custom_ids.add(custom_id)
        prompt_tokens, resolved_max_tokens = tokens.estimate_request_tokens(
            body, default_max_tokens=default_max_tokens
        )
        parsed.append(
            ParsedLine(
                line_index=idx,
                custom_id=custom_id,
                body=body,
                prompt_tokens=prompt_tokens,
                max_tokens=resolved_max_tokens,
            )
        )

    if errors:
        preview = "; ".join(errors[:_MAX_ERRORS_SHOWN])
        extra = len(errors) - _MAX_ERRORS_SHOWN
        more = f" (+{extra} more)" if extra > 0 else ""
        raise InvalidRequestError(
            f"Input file has {len(errors)} invalid line(s): {preview}{more}", param="input_file_id"
        )
    return parsed


# --- Submission. ---


def create_batch(
    db: Session,
    *,
    user: User,
    input_file: FileObject,
    endpoint: str,
    completion_window: str,
    metadata: dict | None,
    parsed_lines: list[ParsedLine],
) -> Batch:
    """Reserves worst-case tokens for every line and, if (and only if) that
    fits the student's budget, creates the batch and its tasks -- all
    landing in the ledger.reserve() commit below as one atomic write. Our
    validation is fully synchronous, so batches go straight from "doesn't
    exist" to "in_progress"; BatchStatus.VALIDATING remains a real status
    (mirroring OpenAI's) for a future async-validation path, but nothing
    here currently leaves a batch parked in it.
    """
    now_ts = _now()
    batch_id = f"batch_{uuid.uuid4().hex}"
    total_reserve = sum(p.reserved_tokens for p in parsed_lines)
    expires_at = now_ts + timedelta(hours=24) if completion_window == "24h" else None

    batch = Batch(
        id=batch_id,
        user_id=user.id,
        input_file_id=input_file.id,
        endpoint=endpoint,
        completion_window=completion_window,
        status=BatchStatus.IN_PROGRESS,
        reserved_tokens=total_reserve,
        request_total=len(parsed_lines),
        metadata_json=metadata,
        created_at=now_ts,
        in_progress_at=now_ts,
        expires_at=expires_at,
    )
    db.add(batch)

    for p in parsed_lines:
        db.add(
            Task(
                batch_id=batch_id,
                line_index=p.line_index,
                custom_id=p.custom_id,
                request_body=p.body,
                reserved_tokens=p.reserved_tokens,
                prompt_tokens_estimate=p.prompt_tokens,
                max_tokens=p.max_tokens,
                status=TaskStatus.PENDING,
            )
        )

    # Raises InsufficientBudgetError (mapped to HTTP 429 by main.py) and
    # leaves nothing committed if this doesn't fit -- see module docstring.
    ledger.reserve(
        db, user, total_reserve, batch_id=batch_id, note=f"batch submit ({len(parsed_lines)} requests)"
    )
    db.refresh(batch)
    return batch


# --- Cancellation / expiry. Both stop a batch early and release whatever
# of its reservation is still outstanding -- the only difference is who
# triggered it and which terminal status/timestamp lands on the batch. ---


def _terminate_batch(
    db: Session, *, user: User, batch: Batch, terminal_status: BatchStatus, note: str
) -> Batch:
    now_ts = _now()
    tasks_to_stop = (
        db.query(Task)
        .filter(Task.batch_id == batch.id, Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        .all()
    )
    release_amount = sum(t.reserved_tokens for t in tasks_to_stop)
    for t in tasks_to_stop:
        t.status = TaskStatus.CANCELLED
        t.finished_at = now_ts

    batch.status = terminal_status
    if terminal_status == BatchStatus.CANCELLED:
        batch.cancelled_at = now_ts
    elif terminal_status == BatchStatus.EXPIRED:
        batch.expired_at = now_ts

    if release_amount > 0:
        ledger.release(db, user, release_amount, batch_id=batch.id, note=note)
    else:
        db.commit()
    db.refresh(batch)
    return batch


def cancel_batch(db: Session, *, user: User, batch: Batch) -> Batch:
    if batch.status not in _CANCELLABLE_STATUSES:
        raise ConflictError(f"Batch '{batch.id}' cannot be cancelled from status '{batch.status}'.")
    return _terminate_batch(
        db, user=user, batch=batch, terminal_status=BatchStatus.CANCELLED, note="batch cancelled"
    )


def expire_batch(db: Session, *, user: User, batch: Batch) -> Batch:
    """Same effect as cancel_batch, triggered by the retention job
    (retention.py) once a batch's completion_window has passed rather
    than by the student. Callers are expected to have already checked
    batch.expires_at -- this doesn't re-check it."""
    if batch.status not in _CANCELLABLE_STATUSES:
        raise ConflictError(f"Batch '{batch.id}' cannot expire from status '{batch.status}'.")
    return _terminate_batch(
        db, user=user, batch=batch, terminal_status=BatchStatus.EXPIRED, note="batch expired"
    )


# --- Task completion sink (called by the M3 dispatcher; exercised
# directly by tests until then). ---


def complete_task(
    db: Session,
    *,
    task: Task,
    response_body: dict,
    prompt_tokens: int,
    completion_tokens: int,
    blob_dir: Path,
    eta_alpha: float = 0.3,
) -> Task:
    now_ts = _now()
    task.status = TaskStatus.COMPLETED
    task.response_body = response_body
    task.prompt_tokens = prompt_tokens
    task.completion_tokens = completion_tokens
    task.finished_at = now_ts

    batch = db.get(Batch, task.batch_id)
    batch.request_completed += 1
    user = db.get(User, batch.user_id)

    # Feeds the M4 ETA model's rolling throughput estimate. A no-op if this
    # task was never actually dispatched (started_at unset) -- see eta.py.
    eta.update_dispatch_stats(db, task=task, alpha=eta_alpha)

    ledger.charge(
        db,
        user,
        reserved_tokens=task.reserved_tokens,
        actual_tokens=prompt_tokens + completion_tokens,
        batch_id=batch.id,
        task_id=task.id,
    )
    _maybe_finalize_batch(db, batch=batch, blob_dir=blob_dir)
    db.refresh(task)
    return task


def fail_task(db: Session, *, task: Task, error: str, blob_dir: Path) -> Task:
    now_ts = _now()
    task.status = TaskStatus.FAILED
    task.error = error
    task.finished_at = now_ts

    batch = db.get(Batch, task.batch_id)
    batch.request_failed += 1
    user = db.get(User, batch.user_id)

    if task.reserved_tokens > 0:
        ledger.release(db, user, task.reserved_tokens, batch_id=batch.id, task_id=task.id, note=error)
    else:
        db.commit()
    _maybe_finalize_batch(db, batch=batch, blob_dir=blob_dir)
    db.refresh(task)
    return task


def _maybe_finalize_batch(db: Session, *, batch: Batch, blob_dir: Path) -> None:
    """Once every task has reached a terminal state, writes the output
    (and, if any tasks failed, error) JSONL files and flips the batch to
    completed. A no-op unless the batch is still in_progress and every
    task has reported in."""
    if batch.status != BatchStatus.IN_PROGRESS:
        return
    if batch.request_completed + batch.request_failed < batch.request_total:
        return

    now_ts = _now()
    batch.status = BatchStatus.FINALIZING
    batch.finalizing_at = now_ts

    batch_tasks = db.query(Task).filter(Task.batch_id == batch.id).order_by(Task.line_index).all()

    output_lines = []
    error_lines = []
    for t in batch_tasks:
        if t.status == TaskStatus.COMPLETED:
            output_lines.append(
                json.dumps(
                    {
                        "id": f"batch_req_{t.id}",
                        "custom_id": t.custom_id,
                        "response": {"status_code": 200, "request_id": t.id, "body": t.response_body},
                        "error": None,
                    }
                )
            )
        elif t.status == TaskStatus.FAILED:
            error_lines.append(
                json.dumps(
                    {
                        "id": f"batch_req_{t.id}",
                        "custom_id": t.custom_id,
                        "response": None,
                        "error": {"message": t.error or "task failed", "code": "task_failed"},
                    }
                )
            )

    output_file_id = new_file_id()
    output_bytes = ("\n".join(output_lines) + "\n").encode("utf-8") if output_lines else b""
    output_path, output_sha = write_blob(blob_dir, output_file_id, output_bytes)
    db.add(
        FileObject(
            id=output_file_id,
            user_id=batch.user_id,
            purpose=FilePurpose.BATCH_OUTPUT,
            filename=f"{batch.id}_output.jsonl",
            path=str(output_path),
            bytes=len(output_bytes),
            sha256=output_sha,
        )
    )
    # Flush the FileObject insert(s) before the batches UPDATE below: without
    # an ORM relationship() between Batch and FileObject, the unit of work
    # has no way to infer that output_file_id/error_file_id depend on rows
    # that don't exist yet, and (with SQLite's immediate FK checking) an
    # UPDATE ordered before the matching INSERT fails the FK constraint.
    db.flush()
    batch.output_file_id = output_file_id

    if error_lines:
        error_file_id = new_file_id()
        error_bytes = ("\n".join(error_lines) + "\n").encode("utf-8")
        error_path, error_sha = write_blob(blob_dir, error_file_id, error_bytes)
        db.add(
            FileObject(
                id=error_file_id,
                user_id=batch.user_id,
                purpose=FilePurpose.BATCH_ERROR,
                filename=f"{batch.id}_errors.jsonl",
                path=str(error_path),
                bytes=len(error_bytes),
                sha256=error_sha,
            )
        )
        db.flush()
        batch.error_file_id = error_file_id

    batch.status = BatchStatus.COMPLETED
    batch.completed_at = now_ts
    db.commit()
