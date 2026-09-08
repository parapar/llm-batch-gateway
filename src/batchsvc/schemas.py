"""Pydantic request/response models for the admin, files, and batches APIs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    full_name: str | None = None


class UserOut(BaseModel):
    id: str
    username: str
    full_name: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    label: str | None = None


class ApiKeyCreated(BaseModel):
    id: str
    key: str  # raw key, shown once
    key_prefix: str
    label: str | None
    created_at: datetime


class ApiKeyOut(BaseModel):
    id: str
    key_prefix: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class BudgetGrant(BaseModel):
    tokens: int = Field(gt=0)
    note: str | None = None


class BudgetOut(BaseModel):
    user_id: str
    granted_tokens: int
    reserved_tokens: int
    used_tokens: int
    available_tokens: int
    updated_at: datetime


class LedgerEntryOut(BaseModel):
    id: str
    entry_type: str
    granted_delta: int
    reserved_delta: int
    used_delta: int
    batch_id: str | None
    task_id: str | None
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- /v1/files, /v1/batches: OpenAI-shaped so the official SDK works
# unmodified against base_url=".../v1". created_at and friends are Unix
# timestamps (ints), matching the real API, not ISO datetimes like the
# admin schemas above. ---


class FileOut(BaseModel):
    id: str
    object: str = "file"
    bytes: int
    created_at: int
    filename: str
    purpose: str


class BatchCreate(BaseModel):
    input_file_id: str
    endpoint: str
    completion_window: str = "24h"
    metadata: dict | None = None


class BatchRequestCounts(BaseModel):
    total: int
    completed: int
    failed: int


class BatchTokenUsage(BaseModel):
    """Non-standard extension (not part of the OpenAI Batch object) so
    students can see accounting impact without a separate call to
    /v1/budget. `reserved` is the worst-case total held for this batch at
    submit time; `consumed` is what's actually been charged so far as
    tasks complete -- the gap between them is released back to the
    student's available budget as the batch finishes."""

    reserved: int
    consumed: int


class BatchOut(BaseModel):
    id: str
    object: str = "batch"
    endpoint: str
    input_file_id: str
    completion_window: str
    status: str
    output_file_id: str | None = None
    error_file_id: str | None = None
    created_at: int
    in_progress_at: int | None = None
    expires_at: int | None = None
    finalizing_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None
    cancelling_at: int | None = None
    cancelled_at: int | None = None
    expired_at: int | None = None
    request_counts: BatchRequestCounts
    metadata: dict | None = None
    x_tokens: BatchTokenUsage


class BatchListOut(BaseModel):
    object: str = "list"
    data: list[BatchOut]
    has_more: bool = False
