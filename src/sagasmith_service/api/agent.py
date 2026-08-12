from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.integrations.agent import AgentRuntime
from sagasmith_service.models import (
    AgentConversation,
    AgentRun,
    AuditEvent,
    CampaignMembershipProjection,
    now_utc,
)
from sagasmith_service.quota import QuotaExceededError, release, reserve, settle
from sagasmith_service.schemas import (
    AgentMessageRequest,
    AgentRunView,
    ConversationCreate,
    ConversationView,
)

router = APIRouter(prefix="/api/campaigns/{campaign_id}/agent", tags=["agent"])


def _membership(
    session: DbSession, campaign_id: str, user_id: str
) -> CampaignMembershipProjection:
    item = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user_id,
            CampaignMembershipProjection.status == "active",
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign membership required")
    return item


@router.post("/conversations", response_model=ConversationView, status_code=status.HTTP_201_CREATED)
def create_conversation(
    campaign_id: str,
    payload: ConversationCreate,
    user: CurrentUser,
    session: DbSession,
) -> ConversationView:
    _membership(session, campaign_id, user.id)
    item = AgentConversation(
        campaign_id=campaign_id,
        user_id=user.id,
        title=payload.title,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="agent.conversation.create",
            subject_type="agent_conversation",
            subject_id=item.id,
            details={"campaign_id": campaign_id},
        )
    )
    session.commit()
    return ConversationView.model_validate(item)


@router.get("/conversations", response_model=list[ConversationView])
def list_conversations(
    campaign_id: str, user: CurrentUser, session: DbSession
) -> list[ConversationView]:
    _membership(session, campaign_id, user.id)
    return [
        ConversationView.model_validate(item)
        for item in session.scalars(
            select(AgentConversation)
            .where(
                AgentConversation.campaign_id == campaign_id,
                AgentConversation.user_id == user.id,
            )
            .order_by(AgentConversation.updated_at.desc())
        ).all()
    ]


@router.get("/conversations/{conversation_id}/runs", response_model=list[AgentRunView])
def list_runs(
    campaign_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
) -> list[AgentRunView]:
    _membership(session, campaign_id, user.id)
    conversation = session.scalar(
        select(AgentConversation.id).where(
            AgentConversation.id == conversation_id,
            AgentConversation.campaign_id == campaign_id,
            AgentConversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    items = session.scalars(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id, AgentRun.user_id == user.id)
        .order_by(AgentRun.created_at)
    ).all()
    return [AgentRunView.model_validate(item) for item in items]


@router.post("/conversations/{conversation_id}/messages", response_model=AgentRunView)
async def send_message(
    campaign_id: str,
    conversation_id: str,
    payload: AgentMessageRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> AgentRunView:
    membership = _membership(session, campaign_id, user.id)
    conversation = session.scalar(
        select(AgentConversation).where(
            AgentConversation.id == conversation_id,
            AgentConversation.campaign_id == campaign_id,
            AgentConversation.user_id == user.id,
            AgentConversation.status == "active",
        )
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    request_hash = hashlib.sha256(payload.content.encode()).hexdigest()
    existing = session.scalar(
        select(AgentRun).where(
            AgentRun.user_id == user.id,
            AgentRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key payload mismatch")
        if existing.status != "completed":
            raise HTTPException(status.HTTP_409_CONFLICT, "agent request is already in progress")
        return AgentRunView.model_validate(existing)
    quota_quantity = Decimal(request.app.state.settings.agent_reservation_tokens)
    try:
        reservation = reserve(
            session,
            user_id=user.id,
            campaign_id=campaign_id,
            metric="llm_tokens",
            quantity=quota_quantity,
            idempotency_key=f"agent-reserve:{idempotency_key}",
            ttl_seconds=300,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    run = AgentRun(
        conversation_id=conversation_id,
        campaign_id=campaign_id,
        user_id=user.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        user_content=payload.content,
    )
    session.add(run)
    session.commit()
    runtime: AgentRuntime = request.app.state.agent_runtime
    try:
        result = await runtime.complete(
            session_id=f"{campaign_id}:{user.id}:{conversation_id}",
            content=payload.content,
            context={
                "campaign_id": campaign_id,
                "principal_id": user.principal_id,
                "campaign_role": membership.role,
            },
        )
    except RuntimeError as exc:
        release(session, reservation.id)
        run.status = "failed"
        run.error_code = "agent_unavailable"
        run.completed_at = now_utc()
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="agent.failed",
                subject_type="agent_run",
                subject_id=run.id,
                details={"campaign_id": campaign_id, "error_code": run.error_code},
            )
        )
        session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    actual = min(result.total_tokens, int(quota_quantity))
    settle(
        session,
        reservation_id=reservation.id,
        quantity=Decimal(actual),
        idempotency_key=f"agent-settle:{idempotency_key}",
        unit="tokens",
        provider="nanobot",
        model=result.model,
        request_id=result.request_id,
    )
    run.assistant_content = result.content
    run.upstream_request_id = result.request_id
    run.model = result.model
    run.prompt_tokens = result.prompt_tokens
    run.completion_tokens = result.completion_tokens
    run.status = "completed"
    run.completed_at = now_utc()
    conversation.updated_at = now_utc()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="agent.complete",
            subject_type="agent_run",
            subject_id=run.id,
            details={"campaign_id": campaign_id, "tokens": actual},
        )
    )
    session.commit()
    return AgentRunView.model_validate(run)
