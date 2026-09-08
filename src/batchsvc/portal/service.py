"""Portal data access: provisioning, dashboard figures, key rotation.

Kept out of routes.py so the interesting logic (what a student is
allowed to see, what happens on first login) is testable without going
through HTTP, matching how batch_ops.py relates to the batches router.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from batchsvc import ledger
from batchsvc.config import PortalConfig
from batchsvc.ldap_auth import LdapIdentity
from batchsvc.models import ApiKey, Batch, Budget, LedgerEntry, LedgerEntryType, User
from batchsvc.security import generate_api_key

logger = logging.getLogger("batchsvc.portal")

RECENT_BATCH_LIMIT = 20
RECENT_LEDGER_LIMIT = 20


class PortalAccessDenied(Exception):
    """Authenticated against the directory, but not allowed a portal
    account here (unknown user with auto-provisioning off, or an account
    an admin has disabled)."""


@dataclass
class BatchRow:
    id: str
    created_at: datetime
    status: str
    request_total: int
    request_completed: int
    request_failed: int
    tokens_consumed: int


@dataclass
class DashboardData:
    user: User
    budget: Budget
    api_key: ApiKey | None
    batches: list[BatchRow] = field(default_factory=list)
    ledger_entries: list[LedgerEntry] = field(default_factory=list)

    @property
    def used_percent(self) -> float:
        if self.budget.granted_tokens <= 0:
            return 0.0
        spent = self.budget.used_tokens + self.budget.reserved_tokens
        return min(100.0, round(spent / self.budget.granted_tokens * 100, 1))


def resolve_user(db: Session, identity: LdapIdentity, config: PortalConfig) -> User:
    """Maps a directory identity onto a batchsvc account, creating one on
    first login when auto-provisioning is on."""
    user = db.query(User).filter(User.username == identity.username).one_or_none()

    if user is None:
        if not config.auto_provision:
            raise PortalAccessDenied(
                f"no account for '{identity.username}' and auto-provisioning is disabled"
            )
        user = User(username=identity.username, full_name=identity.display_name)
        db.add(user)
        db.flush()
        db.add(Budget(user_id=user.id))
        db.commit()
        if config.default_grant_tokens > 0:
            ledger.grant(
                db, user, config.default_grant_tokens, note="automatic grant on first portal login"
            )
        logger.info(
            "provisioned portal account",
            extra={"username": user.username, "granted": config.default_grant_tokens},
        )
        return user

    if not user.is_active:
        raise PortalAccessDenied(f"account '{identity.username}' is disabled")

    # Keep the display name fresh -- it's the directory's to own, not ours.
    if identity.display_name and user.full_name != identity.display_name:
        user.full_name = identity.display_name
        db.commit()
    return user


def load_dashboard(db: Session, user: User) -> DashboardData:
    budget = user.budget or ledger.get_or_create_budget(db, user.id)

    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
        .first()
    )

    batches = (
        db.query(Batch)
        .filter(Batch.user_id == user.id)
        .order_by(Batch.created_at.desc())
        .limit(RECENT_BATCH_LIMIT)
        .all()
    )
    # One grouped query rather than one per batch: this page is polled by
    # a whole class at once.
    consumed_by_batch = dict(
        db.query(LedgerEntry.batch_id, func.coalesce(func.sum(LedgerEntry.used_delta), 0))
        .filter(
            LedgerEntry.user_id == user.id,
            LedgerEntry.entry_type == LedgerEntryType.CHARGE,
            LedgerEntry.batch_id.is_not(None),
        )
        .group_by(LedgerEntry.batch_id)
        .all()
    )

    batch_rows = [
        BatchRow(
            id=b.id,
            created_at=b.created_at,
            status=str(b.status),
            request_total=b.request_total,
            request_completed=b.request_completed,
            request_failed=b.request_failed,
            tokens_consumed=int(consumed_by_batch.get(b.id, 0)),
        )
        for b in batches
    ]

    # Grants and manual adjustments only. The ledger also records a
    # reserve/charge/release per *task*, which for a 500-line batch is 500
    # near-identical rows -- and per-job spend is already the table above.
    # What this panel answers is "where did my allowance come from", which
    # nothing else on the page shows.
    entries = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.user_id == user.id,
            LedgerEntry.entry_type.in_([LedgerEntryType.GRANT, LedgerEntryType.ADJUST]),
        )
        .order_by(LedgerEntry.created_at.desc())
        .limit(RECENT_LEDGER_LIMIT)
        .all()
    )

    return DashboardData(
        user=user, budget=budget, api_key=api_key, batches=batch_rows, ledger_entries=entries
    )


def regenerate_api_key(db: Session, user: User) -> str:
    """Revokes the student's existing keys and issues one new key,
    returning the raw value. That raw value is shown once and never
    stored -- only its sha256 is (see security.py)."""
    now = datetime.now(tz=None).astimezone()
    active = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)).all()
    for key in active:
        key.revoked_at = now

    raw_key, key_prefix, key_hash = generate_api_key()
    db.add(
        ApiKey(user_id=user.id, key_prefix=key_prefix, key_hash=key_hash, label="portal")
    )
    db.commit()
    logger.info(
        "portal issued new api key",
        extra={"username": user.username, "revoked_previous": len(active)},
    )
    return raw_key
