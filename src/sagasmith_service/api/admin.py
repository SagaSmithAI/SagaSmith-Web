from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import AuditEvent, QuotaGrant, User, now_utc
from sagasmith_service.schemas import AdminQuotaGrantRequest, QuotaGrantView

router = APIRouter(prefix="/api/admin", tags=["administration"])


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator required")


@router.post("/users/{user_id}/quota-grants", response_model=QuotaGrantView, status_code=201)
def grant_quota(
    user_id: str,
    payload: AdminQuotaGrantRequest,
    user: CurrentUser,
    session: DbSession,
) -> QuotaGrantView:
    _require_admin(user)
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    start = now_utc()
    item = QuotaGrant(
        user_id=user_id,
        metric=payload.metric,
        quantity=Decimal(payload.quantity),
        period_start=start,
        period_end=start + timedelta(days=payload.valid_days),
        source="admin",
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="quota.grant.admin",
            subject_type="quota_grant",
            subject_id=item.id,
            details={"target_user_id": user_id, "quantity": str(item.quantity)},
        )
    )
    session.commit()
    return QuotaGrantView.model_validate(item)
