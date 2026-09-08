"""Admin API: student accounts, API keys, and budget management.

Everything here requires the admin bearer token (require_admin), which is
separate from student API keys -- see deps.py.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from batchsvc import ledger
from batchsvc.deps import get_db, require_admin
from batchsvc.errors import ConflictError, NotFoundError
from batchsvc.models import ApiKey, Budget, LedgerEntry, User
from batchsvc.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    BudgetGrant,
    BudgetOut,
    LedgerEntryOut,
    UserCreate,
    UserOut,
)
from batchsvc.security import generate_api_key

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _get_user_or_404(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"No user with id '{user_id}'.")
    return user


def _budget_out(budget: Budget) -> BudgetOut:
    return BudgetOut(
        user_id=budget.user_id,
        granted_tokens=budget.granted_tokens,
        reserved_tokens=budget.reserved_tokens,
        used_tokens=budget.used_tokens,
        available_tokens=budget.available_tokens,
        updated_at=budget.updated_at,
    )


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    existing = db.query(User).filter(User.username == payload.username).one_or_none()
    if existing is not None:
        raise ConflictError(f"Username '{payload.username}' is already taken.")
    user = User(username=payload.username, full_name=payload.full_name)
    db.add(user)
    db.flush()
    db.add(Budget(user_id=user.id))
    db.commit()
    db.refresh(user)
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.created_at).all()


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: str, db: Session = Depends(get_db)) -> User:
    return _get_user_or_404(db, user_id)


@router.post("/users/{user_id}/disable", response_model=UserOut)
def disable_user(user_id: str, db: Session = Depends(get_db)) -> User:
    user = _get_user_or_404(db, user_id)
    user.is_active = False
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: str, db: Session = Depends(get_db)) -> User:
    user = _get_user_or_404(db, user_id)
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
def create_api_key(user_id: str, payload: ApiKeyCreate, db: Session = Depends(get_db)) -> ApiKeyCreated:
    user = _get_user_or_404(db, user_id)
    raw_key, key_prefix, key_hash = generate_api_key()
    api_key = ApiKey(user_id=user.id, key_prefix=key_prefix, key_hash=key_hash, label=payload.label)
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return ApiKeyCreated(
        id=api_key.id,
        key=raw_key,
        key_prefix=api_key.key_prefix,
        label=api_key.label,
        created_at=api_key.created_at,
    )


@router.get("/users/{user_id}/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(user_id: str, db: Session = Depends(get_db)) -> list[ApiKey]:
    _get_user_or_404(db, user_id)
    return db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at).all()


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, db: Session = Depends(get_db)) -> None:
    from datetime import datetime

    api_key = db.get(ApiKey, key_id)
    if api_key is None:
        raise NotFoundError(f"No API key with id '{key_id}'.")
    if not api_key.is_revoked:
        api_key.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/users/{user_id}/budget", response_model=BudgetOut)
def get_budget(user_id: str, db: Session = Depends(get_db)) -> BudgetOut:
    user = _get_user_or_404(db, user_id)
    budget = user.budget or ledger.get_or_create_budget(db, user.id)
    return _budget_out(budget)


@router.post("/users/{user_id}/budget/grant", response_model=BudgetOut)
def grant_budget(user_id: str, payload: BudgetGrant, db: Session = Depends(get_db)) -> BudgetOut:
    user = _get_user_or_404(db, user_id)
    budget = ledger.grant(db, user, payload.tokens, note=payload.note)
    return _budget_out(budget)


@router.get("/users/{user_id}/ledger", response_model=list[LedgerEntryOut])
def get_ledger(user_id: str, limit: int = 100, db: Session = Depends(get_db)) -> list[LedgerEntry]:
    _get_user_or_404(db, user_id)
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.user_id == user_id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(limit)
        .all()
    )


@router.post("/users/{user_id}/budget/reconcile", response_model=BudgetOut)
def reconcile_budget(user_id: str, db: Session = Depends(get_db)) -> BudgetOut:
    """Recompute granted/reserved/used from the ledger and, if the
    materialized Budget row has drifted, correct it. Should always be a
    no-op in normal operation -- exposed as an admin safety valve."""
    user = _get_user_or_404(db, user_id)
    granted, reserved, used = ledger.recompute_budget(db, user_id)
    budget = user.budget or ledger.get_or_create_budget(db, user.id)
    if (budget.granted_tokens, budget.reserved_tokens, budget.used_tokens) != (granted, reserved, used):
        budget.granted_tokens, budget.reserved_tokens, budget.used_tokens = granted, reserved, used
        db.commit()
        db.refresh(budget)
    return _budget_out(budget)
