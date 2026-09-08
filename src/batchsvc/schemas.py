"""Pydantic request/response models for the admin and misc APIs.

Batch/file schemas (OpenAI-shaped) land in M2 alongside their router.
"""

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
