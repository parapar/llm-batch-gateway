"""OpenAI-compatible /v1/batches: submit, poll status, list, cancel.

Results are downloaded through the existing /v1/files/{id}/content route
once output_file_id (and, if any lines failed, error_file_id) are set --
there's no separate results endpoint, matching the real API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from batchsvc import batch_ops, eta
from batchsvc.blobs import read_blob
from batchsvc.config import Settings
from batchsvc.deps import get_current_user, get_db, get_settings
from batchsvc.errors import InvalidRequestError
from batchsvc.models import Batch, FilePurpose, LedgerEntry, LedgerEntryType, User
from batchsvc.schemas import (
    BatchCreate,
    BatchEtaOut,
    BatchListOut,
    BatchOut,
    BatchRequestCounts,
    BatchTokenUsage,
)

router = APIRouter(tags=["batches"])


def _ts(dt) -> int | None:  # noqa: ANN001
    return int(dt.timestamp()) if dt is not None else None


def _tokens_consumed(db: Session, batch_id: str) -> int:
    total = (
        db.query(func.coalesce(func.sum(LedgerEntry.used_delta), 0))
        .filter(LedgerEntry.batch_id == batch_id, LedgerEntry.entry_type == LedgerEntryType.CHARGE)
        .scalar()
    )
    return int(total or 0)


def _batch_out(db: Session, settings: Settings, batch: Batch) -> BatchOut:
    estimate = eta.estimate_batch_eta(db, settings, batch)
    return BatchOut(
        id=batch.id,
        endpoint=batch.endpoint,
        input_file_id=batch.input_file_id,
        completion_window=batch.completion_window,
        status=batch.status,
        output_file_id=batch.output_file_id,
        error_file_id=batch.error_file_id,
        created_at=_ts(batch.created_at),
        in_progress_at=_ts(batch.in_progress_at),
        expires_at=_ts(batch.expires_at),
        finalizing_at=_ts(batch.finalizing_at),
        completed_at=_ts(batch.completed_at),
        failed_at=_ts(batch.failed_at),
        cancelled_at=_ts(batch.cancelled_at),
        expired_at=_ts(batch.expired_at),
        request_counts=BatchRequestCounts(
            total=batch.request_total, completed=batch.request_completed, failed=batch.request_failed
        ),
        metadata=batch.metadata_json,
        x_tokens=BatchTokenUsage(reserved=batch.reserved_tokens, consumed=_tokens_consumed(db, batch.id)),
        x_eta=BatchEtaOut(
            estimated_seconds_remaining=estimate.estimated_seconds_remaining,
            estimated_completion_at=_ts(estimate.estimated_completion_at),
            queue_position=estimate.queue_position,
            confidence=estimate.confidence,
        ),
    )


@router.post("/v1/batches", response_model=BatchOut, status_code=201)
def create_batch(
    payload: BatchCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BatchOut:
    if payload.endpoint != batch_ops.SUPPORTED_ENDPOINT:
        raise InvalidRequestError(
            f"Unsupported endpoint '{payload.endpoint}'. This server only serves "
            f"'{batch_ops.SUPPORTED_ENDPOINT}' (it hosts a single model).",
            param="endpoint",
        )
    if payload.completion_window not in batch_ops.SUPPORTED_COMPLETION_WINDOWS:
        raise InvalidRequestError("completion_window must be '24h'.", param="completion_window")

    input_file = batch_ops.get_owned_file_or_404(db, user, payload.input_file_id)
    if input_file.purpose != FilePurpose.BATCH_INPUT:
        raise InvalidRequestError(
            f"File '{input_file.id}' was not uploaded with purpose='batch'.", param="input_file_id"
        )

    raw = read_blob(input_file.path)
    parsed_lines = batch_ops.parse_batch_input(
        raw, endpoint=payload.endpoint, default_max_tokens=settings.default_max_tokens
    )
    batch = batch_ops.create_batch(
        db,
        user=user,
        input_file=input_file,
        endpoint=payload.endpoint,
        completion_window=payload.completion_window,
        metadata=payload.metadata,
        parsed_lines=parsed_lines,
    )
    return _batch_out(db, settings, batch)


@router.get("/v1/batches/{batch_id}", response_model=BatchOut)
def get_batch(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BatchOut:
    batch = batch_ops.get_owned_batch_or_404(db, user, batch_id)
    return _batch_out(db, settings, batch)


@router.get("/v1/batches", response_model=BatchListOut)
def list_batches(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BatchListOut:
    limit = max(1, min(limit, 100))
    rows = (
        db.query(Batch)
        .filter(Batch.user_id == user.id)
        .order_by(Batch.created_at.desc())
        .limit(limit)
        .all()
    )
    return BatchListOut(data=[_batch_out(db, settings, b) for b in rows])


@router.post("/v1/batches/{batch_id}/cancel", response_model=BatchOut)
def cancel_batch(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BatchOut:
    batch = batch_ops.get_owned_batch_or_404(db, user, batch_id)
    batch = batch_ops.cancel_batch(db, user=user, batch=batch)
    return _batch_out(db, settings, batch)
