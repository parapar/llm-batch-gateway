"""SQLAlchemy ORM models.

Tables cover the full plan (docs/PLAN.md), but M1 only exercises users,
api_keys, budgets, and ledger_entries. files/batches/tasks/nodes are
defined now so the schema doesn't churn under M2/M3, but stay unused
until then.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")
    budget: Mapped[Budget | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class Budget(Base):
    """Materialized view of a user's token budget.

    Always equal to replaying that user's ledger_entries from zero --
    see ledger.py:recompute_budget, exercised by the accounting invariant
    tests. Kept as a table (rather than computed on every read) so status
    checks and reservation checks are O(1).
    """

    __tablename__ = "budgets"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    granted_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    user: Mapped[User] = relationship(back_populates="budget")

    @property
    def available_tokens(self) -> int:
        return self.granted_tokens - self.reserved_tokens - self.used_tokens


class LedgerEntryType(enum.StrEnum):
    GRANT = "grant"
    RESERVE = "reserve"
    RELEASE = "release"
    CHARGE = "charge"
    ADJUST = "adjust"


class LedgerEntry(Base):
    """Append-only audit trail. Never updated or deleted.

    Each row records the deltas it applied to the user's Budget row, so
    the full history can always reconstruct (and verify) the current
    balance. batch_id/task_id are plain strings (not FKs) since files.py
    Table objects for batches/tasks don't exist until M2/M3 -- entries
    written in M1 (grants/manual adjustments) simply leave them null.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    entry_type: Mapped[LedgerEntryType] = mapped_column(String(16))
    granted_delta: Mapped[int] = mapped_column(Integer, default=0)
    reserved_delta: Mapped[int] = mapped_column(Integer, default=0)
    used_delta: Mapped[int] = mapped_column(Integer, default=0)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    user: Mapped[User] = relationship(back_populates="ledger_entries")


# --- Defined for schema stability across M2/M3; unused until then. ---


class FilePurpose(enum.StrEnum):
    BATCH_INPUT = "batch"
    BATCH_OUTPUT = "batch_output"
    BATCH_ERROR = "batch_error"


class FileObject(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: f"file_{_uuid()}")
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[FilePurpose] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(256))
    path: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BatchStatus(enum.StrEnum):
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: f"batch_{_uuid()}")
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    input_file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    output_file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"), nullable=True)
    error_file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(64), default="/v1/chat/completions")
    completion_window: Mapped[str] = mapped_column(String(16), default="24h")
    status: Mapped[BatchStatus] = mapped_column(String(16), default=BatchStatus.VALIDATING)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    request_total: Mapped[int] = mapped_column(Integer, default=0)
    request_completed: Mapped[int] = mapped_column(Integer, default=0)
    request_failed: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    in_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalizing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by the M5 retention job (retention.py) once this batch's result
    # files have been deleted past result_retention_days. output_file_id/
    # error_file_id are nulled out at the same time -- GET /v1/files/{id}
    # on a purged file 404s naturally since the FileObject row is deleted
    # too, not because of this flag; it exists for observability/audit
    # (so a purged batch's own record explains why its files are gone).
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    custom_id: Mapped[str] = mapped_column(String(256))
    request_body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[TaskStatus] = mapped_column(String(16), default=TaskStatus.PENDING, index=True)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Pre-submission worst-case estimate (batch_ops.parse_batch_input), split
    # out from reserved_tokens (their sum) so the M4 ETA model can use
    # *expected* rather than worst-case output length per pending task.
    prompt_tokens_estimate: Mapped[int] = mapped_column(Integer, default=0)
    max_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Actual usage, filled in once the task completes.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    node_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeHealth(enum.StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


class Node(Base):
    __tablename__ = "nodes"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(256))
    parallel_slots: Mapped[int] = mapped_column(Integer, default=4)
    health: Mapped[NodeHealth] = mapped_column(String(16), default=NodeHealth.HEALTHY)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    tokens_per_second_ewma: Mapped[float | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DispatchStat(Base):
    """Single global row (id="global") tracking the rolling cluster
    throughput used for M4's ETA estimates -- see eta.py. Deliberately
    cluster-wide rather than per-node: all nodes in a deployment serve
    the same model (docs/PLAN.md), so per-task throughput is treated as
    one shared distribution rather than tracked separately per node.
    """

    __tablename__ = "dispatch_stats"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: "global")
    # EWMA of (weighted tokens) / (wall-clock seconds) for one task on one
    # node's slot -- multiply by healthy concurrent slots for cluster
    # throughput. "Weighted tokens" = prompt_tokens/10 + completion_tokens
    # (eta.PROMPT_TOKEN_WEIGHT), since prefill is much cheaper per token
    # than generation.
    tokens_per_second_ewma: Mapped[float | None] = mapped_column(nullable=True)
    # EWMA of completed tasks' actual completion_tokens, used as the
    # "expected output length" for a pending task instead of its
    # worst-case max_tokens.
    avg_completion_tokens_ewma: Mapped[float | None] = mapped_column(nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
