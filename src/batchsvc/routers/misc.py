"""Health check and the one non-OpenAI-standard student-facing endpoint:
GET /v1/budget, so students can check their own remaining tokens without
an admin key. Extra fields on a namespace OpenAI doesn't use, so it
doesn't collide with SDK behavior.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from batchsvc import ledger
from batchsvc.deps import get_current_user, get_db
from batchsvc.models import User
from batchsvc.schemas import BudgetOut

router = APIRouter(tags=["misc"])


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/v1/budget", response_model=BudgetOut)
def my_budget(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> BudgetOut:
    budget = user.budget or ledger.get_or_create_budget(db, user.id)
    return BudgetOut(
        user_id=budget.user_id,
        granted_tokens=budget.granted_tokens,
        reserved_tokens=budget.reserved_tokens,
        used_tokens=budget.used_tokens,
        available_tokens=budget.available_tokens,
        updated_at=budget.updated_at,
    )
