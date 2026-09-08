"""Token budget accounting.

Every mutation to a user's budget goes through exactly one of the
functions below, and every one of them writes an append-only
LedgerEntry alongside updating the materialized Budget row, in the same
DB transaction. That gives two guarantees:

  1. Budget can be recomputed from history alone (recompute_budget),
     which the invariant tests use to catch any drift between the
     ledger and the materialized row.
  2. available_tokens = granted - reserved - used can never go negative:
     reserve() checks it atomically before writing.

Concurrency: SQLite serializes writers at the database level, but that
alone doesn't make "check available, then update" atomic across two
Python threads/requests -- a second thread could read the same stale
available_tokens before the first commits. We close that gap with a
process-wide lock around the check-then-write sequence. This is
sufficient for a single API process (M1's deployment target) and is
flagged here because M3's dispatcher, which also charges/releases,
must go through this same module rather than writing Budget rows
directly.
"""

from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from batchsvc.models import Budget, LedgerEntry, LedgerEntryType, User

_budget_lock = threading.Lock()


class InsufficientBudgetError(Exception):
    def __init__(self, requested: int, available: int):
        self.requested = requested
        self.available = available
        super().__init__(f"requested {requested} tokens but only {available} available")


def get_or_create_budget(db: Session, user_id: str) -> Budget:
    budget = db.get(Budget, user_id)
    if budget is None:
        budget = Budget(user_id=user_id, granted_tokens=0, reserved_tokens=0, used_tokens=0)
        db.add(budget)
        db.flush()
    return budget


def grant(db: Session, user: User, tokens: int, *, note: str | None = None) -> Budget:
    """Add to a user's granted budget (e.g. instructor allowance)."""
    if tokens <= 0:
        raise ValueError("grant amount must be positive")
    with _budget_lock:
        budget = get_or_create_budget(db, user.id)
        budget.granted_tokens += tokens
        db.add(
            LedgerEntry(
                user_id=user.id,
                entry_type=LedgerEntryType.GRANT,
                granted_delta=tokens,
                note=note,
            )
        )
        db.commit()
        db.refresh(budget)
        return budget


def reserve(
    db: Session,
    user: User,
    tokens: int,
    *,
    batch_id: str | None = None,
    note: str | None = None,
) -> Budget:
    """Reserve worst-case tokens for a batch submission.

    Raises InsufficientBudgetError (caller maps this to HTTP 429) and
    leaves the budget untouched if it doesn't fit.
    """
    if tokens <= 0:
        raise ValueError("reserve amount must be positive")
    with _budget_lock:
        budget = get_or_create_budget(db, user.id)
        available = budget.available_tokens
        if tokens > available:
            raise InsufficientBudgetError(requested=tokens, available=available)
        budget.reserved_tokens += tokens
        db.add(
            LedgerEntry(
                user_id=user.id,
                entry_type=LedgerEntryType.RESERVE,
                reserved_delta=tokens,
                batch_id=batch_id,
                note=note,
            )
        )
        db.commit()
        db.refresh(budget)
        return budget


def release(
    db: Session,
    user: User,
    tokens: int,
    *,
    batch_id: str | None = None,
    task_id: str | None = None,
    note: str | None = None,
) -> Budget:
    """Give back an unused reservation (batch cancelled/expired, or the
    unused tail of a per-task reservation after charge())."""
    if tokens <= 0:
        raise ValueError("release amount must be positive")
    with _budget_lock:
        budget = get_or_create_budget(db, user.id)
        release_amount = min(tokens, budget.reserved_tokens)
        budget.reserved_tokens -= release_amount
        db.add(
            LedgerEntry(
                user_id=user.id,
                entry_type=LedgerEntryType.RELEASE,
                reserved_delta=-release_amount,
                batch_id=batch_id,
                task_id=task_id,
                note=note,
            )
        )
        db.commit()
        db.refresh(budget)
        return budget


def charge(
    db: Session,
    user: User,
    *,
    reserved_tokens: int,
    actual_tokens: int,
    batch_id: str | None = None,
    task_id: str | None = None,
    note: str | None = None,
) -> Budget:
    """Settle one task's reservation against its real usage: moves
    `reserved_tokens` out of `reserved` (that was this task's worst-case
    hold), and moves `actual_tokens` into `used`. actual_tokens is
    normally <= reserved_tokens; if a task somehow used more (should not
    happen since generation is capped by max_tokens), the excess is still
    charged to `used`, which can push available_tokens negative -- this
    is intentionally allowed here (it is a bookkeeping settlement of work
    already done, not a new reservation) but should be rare enough to be
    worth alerting on in production.
    """
    if reserved_tokens < 0 or actual_tokens < 0:
        raise ValueError("token amounts must be non-negative")
    with _budget_lock:
        budget = get_or_create_budget(db, user.id)
        released = min(reserved_tokens, budget.reserved_tokens)
        budget.reserved_tokens -= released
        budget.used_tokens += actual_tokens
        db.add(
            LedgerEntry(
                user_id=user.id,
                entry_type=LedgerEntryType.CHARGE,
                reserved_delta=-released,
                used_delta=actual_tokens,
                batch_id=batch_id,
                task_id=task_id,
                note=note,
            )
        )
        db.commit()
        db.refresh(budget)
        return budget


def adjust(
    db: Session,
    user: User,
    *,
    granted_delta: int = 0,
    reserved_delta: int = 0,
    used_delta: int = 0,
    note: str,
) -> Budget:
    """Manual admin correction. Unlike the other entry points this can
    move any column in either direction -- used for fixing mistakes, not
    normal batch flow."""
    with _budget_lock:
        budget = get_or_create_budget(db, user.id)
        budget.granted_tokens += granted_delta
        budget.reserved_tokens += reserved_delta
        budget.used_tokens += used_delta
        db.add(
            LedgerEntry(
                user_id=user.id,
                entry_type=LedgerEntryType.ADJUST,
                granted_delta=granted_delta,
                reserved_delta=reserved_delta,
                used_delta=used_delta,
                note=note,
            )
        )
        db.commit()
        db.refresh(budget)
        return budget


def recompute_budget(db: Session, user_id: str) -> tuple[int, int, int]:
    """Replay every ledger entry for a user from zero. Used by tests (and
    could back an admin /reconcile endpoint) to verify the materialized
    Budget row hasn't drifted from the audit trail."""
    granted = reserved = used = 0
    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.user_id == user_id)
        .order_by(LedgerEntry.created_at, LedgerEntry.id)
        .all()
    )
    for e in entries:
        granted += e.granted_delta
        reserved += e.reserved_delta
        used += e.used_delta
    return granted, reserved, used
