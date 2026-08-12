from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import AuditEvent
from sagasmith_service.schemas import AuditEventView

router = APIRouter(prefix="/api/admin/audit-events", tags=["administration"])


@router.get("", response_model=list[AuditEventView])
def list_audit_events(
    user: CurrentUser,
    session: DbSession,
    action: Annotated[str | None, Query(max_length=100)] = None,
    subject_type: Annotated[str | None, Query(max_length=50)] = None,
    subject_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEventView]:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator required")
    statement = select(AuditEvent)
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    if subject_type is not None:
        statement = statement.where(AuditEvent.subject_type == subject_type)
    if subject_id is not None:
        statement = statement.where(AuditEvent.subject_id == subject_id)
    items = session.scalars(statement.order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return [AuditEventView.model_validate(item) for item in items]
