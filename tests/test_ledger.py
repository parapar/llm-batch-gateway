"""Accounting invariant tests: the whole point of the ledger design is
that granted - reserved - used is derivable from the ledger_entries audit
trail alone, and never goes negative on reservation. These tests exercise
that directly against ledger.py, without going through HTTP.
"""

from __future__ import annotations

import pytest

from batchsvc import ledger
from batchsvc.db import Database
from batchsvc.ledger import InsufficientBudgetError
from batchsvc.models import User


def _make_user(db: Database, username: str = "alice") -> User:
    with db.session_scope() as session:
        user = User(username=username)
        session.add(user)
        session.commit()
        session.refresh(user)
        # detach a plain copy of the id; the ORM object itself is bound to
        # this closed session, so callers re-fetch by id.
        return user


def _reload(db: Database, user_id: str) -> User:
    with db.session_scope() as session:
        return session.get(User, user_id)


def test_grant_increases_available(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        budget = ledger.grant(session, u, 1000)
        assert budget.granted_tokens == 1000
        assert budget.available_tokens == 1000


def test_reserve_reduces_available_without_touching_granted(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 1000)
        budget = ledger.reserve(session, u, 400, batch_id="batch_1")
        assert budget.granted_tokens == 1000
        assert budget.reserved_tokens == 400
        assert budget.available_tokens == 600


def test_reserve_rejects_when_insufficient_and_leaves_budget_unchanged(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 100)
        with pytest.raises(InsufficientBudgetError) as exc_info:
            ledger.reserve(session, u, 101, batch_id="batch_1")
        assert exc_info.value.requested == 101
        assert exc_info.value.available == 100

        # Budget must be untouched by the failed reservation.
        budget = ledger.get_or_create_budget(session, u.id)
        assert budget.reserved_tokens == 0
        assert budget.available_tokens == 100


def test_reserve_exactly_at_available_boundary_succeeds(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 100)
        budget = ledger.reserve(session, u, 100, batch_id="batch_1")
        assert budget.available_tokens == 0


def test_charge_settles_reservation_against_actual_usage(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 1000)
        ledger.reserve(session, u, 500, batch_id="batch_1")
        # Task reserved 500 worst-case (e.g. max_tokens), actually used 320.
        budget = ledger.charge(
            session, u, reserved_tokens=500, actual_tokens=320, batch_id="batch_1", task_id="t1"
        )
        assert budget.reserved_tokens == 0
        assert budget.used_tokens == 320
        assert budget.available_tokens == 680  # the unused 180 came back


def test_release_returns_unused_reservation(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 1000)
        ledger.reserve(session, u, 500, batch_id="batch_1")
        budget = ledger.release(session, u, 500, batch_id="batch_1", note="batch cancelled")
        assert budget.reserved_tokens == 0
        assert budget.available_tokens == 1000


def test_release_never_goes_negative_even_if_over_requested(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 1000)
        ledger.reserve(session, u, 100, batch_id="batch_1")
        # Ask to release more than was reserved -- should clamp, not go negative.
        budget = ledger.release(session, u, 9999, batch_id="batch_1")
        assert budget.reserved_tokens == 0
        assert budget.available_tokens == 1000


def test_full_batch_lifecycle_reconciles_to_zero_reserved(db: Database):
    """Reserve worst-case for N tasks, charge each with real (lower)
    usage, and confirm nothing is left dangling in `reserved`."""
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 10_000)

        per_task_reserve = 200
        n_tasks = 5
        ledger.reserve(session, u, per_task_reserve * n_tasks, batch_id="batch_x")

        total_actual = 0
        for i in range(n_tasks):
            actual = 120 + i * 10  # all comfortably under the 200 reservation
            total_actual += actual
            ledger.charge(
                session, u, reserved_tokens=per_task_reserve, actual_tokens=actual,
                batch_id="batch_x", task_id=f"t{i}",
            )

        budget = ledger.get_or_create_budget(session, u.id)
        assert budget.reserved_tokens == 0
        assert budget.used_tokens == total_actual
        assert budget.available_tokens == 10_000 - total_actual


def test_recompute_budget_matches_materialized_row(db: Database):
    """The ledger is the source of truth: replaying it must reproduce
    exactly what's in the Budget row after an arbitrary sequence of ops."""
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 5000)
        ledger.reserve(session, u, 1000, batch_id="b1")
        ledger.charge(session, u, reserved_tokens=300, actual_tokens=250, batch_id="b1", task_id="t1")
        ledger.release(session, u, 700, batch_id="b1")
        ledger.adjust(session, u, used_delta=-10, note="manual correction")

        budget = ledger.get_or_create_budget(session, u.id)
        granted, reserved, used = ledger.recompute_budget(session, u.id)
        assert (granted, reserved, used) == (
            budget.granted_tokens,
            budget.reserved_tokens,
            budget.used_tokens,
        )


def test_ledger_is_append_only_and_ordered(db: Database):
    user = _make_user(db)
    with db.session_scope() as session:
        u = session.get(User, user.id)
        ledger.grant(session, u, 100)
        ledger.grant(session, u, 50)
        ledger.reserve(session, u, 30, batch_id="b1")

        entries = (
            session.query(ledger.LedgerEntry)
            .filter(ledger.LedgerEntry.user_id == u.id)
            .order_by(ledger.LedgerEntry.created_at, ledger.LedgerEntry.id)
            .all()
        )
        assert [e.entry_type for e in entries] == ["grant", "grant", "reserve"]
        assert [e.granted_delta for e in entries] == [100, 50, 0]
        assert [e.reserved_delta for e in entries] == [0, 0, 30]


def test_two_users_budgets_are_independent(db: Database):
    alice = _make_user(db, "alice2")
    bob = _make_user(db, "bob2")
    with db.session_scope() as session:
        a = session.get(User, alice.id)
        b = session.get(User, bob.id)
        ledger.grant(session, a, 1000)
        ledger.grant(session, b, 200)
        ledger.reserve(session, a, 900, batch_id="ba")
        with pytest.raises(InsufficientBudgetError):
            ledger.reserve(session, b, 201, batch_id="bb")

        budget_a = ledger.get_or_create_budget(session, a.id)
        budget_b = ledger.get_or_create_budget(session, b.id)
        assert budget_a.available_tokens == 100
        assert budget_b.available_tokens == 200
