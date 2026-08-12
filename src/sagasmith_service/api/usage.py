from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import UsageLedger
from sagasmith_service.quota import balance
from sagasmith_service.schemas import QuotaBalanceView, UsageLedgerView

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/balance", response_model=QuotaBalanceView)
def get_balance(user: CurrentUser, session: DbSession) -> QuotaBalanceView:
    current = balance(session, user.id, "llm_tokens")
    return QuotaBalanceView(
        metric=current.metric,
        granted=current.granted,
        used=current.used,
        reserved=current.reserved,
        available=current.available,
    )


@router.get("/ledger", response_model=list[UsageLedgerView])
def get_ledger(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[UsageLedgerView]:
    items = session.scalars(
        select(UsageLedger)
        .where(UsageLedger.user_id == user.id)
        .order_by(UsageLedger.occurred_at.desc())
        .limit(limit)
    ).all()
    return [UsageLedgerView.model_validate(item) for item in items]
