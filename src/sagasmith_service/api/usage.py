from __future__ import annotations

from fastapi import APIRouter

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.quota import balance
from sagasmith_service.schemas import QuotaBalanceView

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
