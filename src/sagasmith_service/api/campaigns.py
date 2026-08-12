from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.integrations.dnd_mcp import DndRuntime
from sagasmith_service.models import (
    ActorBindingProjection,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignProjection,
    JoinRequest,
    User,
    now_utc,
)
from sagasmith_service.schemas import (
    ActorBindingRequest,
    ActorBindingView,
    CampaignCreateRequest,
    CampaignView,
    JoinDecisionRequest,
    JoinRequestCreate,
    JoinRequestView,
    MembershipView,
)


def _runtime(request: Request) -> DndRuntime:
    return request.app.state.dnd_runtime


def _campaign_id(receipt: dict[str, Any]) -> str:
    candidates = [receipt, receipt.get("result", {}), receipt.get("campaign", {})]
    for candidate in candidates:
        if isinstance(candidate, dict):
            value = candidate.get("id") or candidate.get("campaign_id")
            if value:
                return str(value)
    raise RuntimeError("D&D MCP campaign receipt has no campaign id")


def _membership(session: DbSession, campaign_id: str, user_id: str) -> CampaignMembershipProjection:
    membership = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user_id,
            CampaignMembershipProjection.status == "active",
        )
    )
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign membership required")
    return membership


router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignView, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreateRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> CampaignView:
    existing = session.scalar(
        select(CampaignProjection).where(
            CampaignProjection.owner_user_id == user.id,
            CampaignProjection.mcp_receipt["service_idempotency_key"].as_string()
            == idempotency_key,
        )
    )
    if existing is not None:
        return CampaignView.model_validate(existing)
    try:
        receipt = await _runtime(request).create_campaign(
            name=payload.name,
            description=payload.description,
            edition=payload.edition,
            locale=payload.locale,
            advancement_mode=payload.advancement_mode,
            principal_id=user.principal_id,
            idempotency_key=f"service:{user.id}:{idempotency_key}",
        )
        campaign_id = _campaign_id(receipt)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    stored_receipt = {**receipt, "service_idempotency_key": idempotency_key}
    campaign = CampaignProjection(
        id=campaign_id,
        name=payload.name,
        owner_user_id=user.id,
        visibility=payload.visibility,
        mcp_receipt=stored_receipt,
    )
    session.add(campaign)
    session.flush()
    session.add(
        CampaignMembershipProjection(
            campaign_id=campaign_id,
            user_id=user.id,
            role="owner",
            mcp_receipt=stored_receipt,
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.create",
            subject_type="campaign",
            subject_id=campaign_id,
            details={"idempotency_key": idempotency_key},
        )
    )
    session.commit()
    return CampaignView.model_validate(campaign)


@router.get("", response_model=list[CampaignView])
def list_campaigns(user: CurrentUser, session: DbSession) -> list[CampaignView]:
    items = session.scalars(
        select(CampaignProjection)
        .join(CampaignMembershipProjection)
        .where(
            CampaignMembershipProjection.user_id == user.id,
            CampaignMembershipProjection.status == "active",
        )
        .order_by(CampaignProjection.updated_at.desc())
    ).all()
    return [CampaignView.model_validate(item) for item in items]


@router.get("/{campaign_id}/members", response_model=list[MembershipView])
def list_members(
    campaign_id: str, user: CurrentUser, session: DbSession
) -> list[MembershipView]:
    _membership(session, campaign_id, user.id)
    rows = session.execute(
        select(CampaignMembershipProjection, User)
        .join(User, User.id == CampaignMembershipProjection.user_id)
        .where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.status == "active",
        )
    ).all()
    return [
        MembershipView(
            user_id=member.user_id,
            display_name=member_user.display_name,
            role=member.role,
            status=member.status,
        )
        for member, member_user in rows
    ]


@router.post(
    "/{campaign_id}/join-requests",
    response_model=JoinRequestView,
    status_code=status.HTTP_201_CREATED,
)
def request_join(
    campaign_id: str,
    payload: JoinRequestCreate,
    user: CurrentUser,
    session: DbSession,
) -> JoinRequestView:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None or campaign.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    if session.scalar(
        select(CampaignMembershipProjection.id).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user.id,
            CampaignMembershipProjection.status == "active",
        )
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "already a campaign member")
    existing = session.scalar(
        select(JoinRequest).where(
            JoinRequest.campaign_id == campaign_id,
            JoinRequest.applicant_user_id == user.id,
            JoinRequest.status == "pending",
        )
    )
    if existing is not None:
        return JoinRequestView.model_validate(existing)
    item = JoinRequest(
        campaign_id=campaign_id,
        applicant_user_id=user.id,
        requested_role="player",
        message=payload.message,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.join.request",
            subject_type="join_request",
            subject_id=item.id,
            details={"campaign_id": campaign_id},
        )
    )
    session.commit()
    return JoinRequestView.model_validate(item)


@router.get("/{campaign_id}/join-requests", response_model=list[JoinRequestView])
def list_join_requests(
    campaign_id: str, user: CurrentUser, session: DbSession
) -> list[JoinRequestView]:
    member = _membership(session, campaign_id, user.id)
    if member.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    return [
        JoinRequestView.model_validate(item)
        for item in session.scalars(
            select(JoinRequest)
            .where(JoinRequest.campaign_id == campaign_id)
            .order_by(JoinRequest.created_at.desc())
        ).all()
    ]


@router.post(
    "/{campaign_id}/join-requests/{join_request_id}/decision",
    response_model=JoinRequestView,
)
async def decide_join_request(
    campaign_id: str,
    join_request_id: str,
    payload: JoinDecisionRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> JoinRequestView:
    reviewer = _membership(session, campaign_id, user.id)
    if reviewer.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    item = session.scalar(
        select(JoinRequest)
        .where(JoinRequest.id == join_request_id, JoinRequest.campaign_id == campaign_id)
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "join request not found")
    if item.status != "pending":
        if item.status == payload.decision:
            return JoinRequestView.model_validate(item)
        raise HTTPException(status.HTTP_409_CONFLICT, "join request already decided")
    receipt: dict[str, Any] = {}
    if payload.decision == "approved":
        applicant = session.get(User, item.applicant_user_id)
        if applicant is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "applicant no longer exists")
        try:
            receipt = await _runtime(request).grant_campaign_access(
                campaign_id=campaign_id,
                principal_id=applicant.principal_id,
                role="player",
                by_principal_id=user.principal_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        member = session.scalar(
            select(CampaignMembershipProjection).where(
                CampaignMembershipProjection.campaign_id == campaign_id,
                CampaignMembershipProjection.user_id == applicant.id,
            )
        )
        if member is None:
            member = CampaignMembershipProjection(
                campaign_id=campaign_id,
                user_id=applicant.id,
                role="player",
            )
            session.add(member)
        member.status = "active"
        member.role = "player"
        member.mcp_receipt = receipt
    item.status = payload.decision
    item.reviewed_by_user_id = user.id
    item.reviewed_at = now_utc()
    item.mcp_receipt = receipt
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action=f"campaign.join.{payload.decision}",
            subject_type="join_request",
            subject_id=item.id,
            details={"campaign_id": campaign_id},
        )
    )
    session.commit()
    return JoinRequestView.model_validate(item)


@router.put("/{campaign_id}/actors/{actor_id}/binding", response_model=ActorBindingView)
async def bind_actor(
    campaign_id: str,
    actor_id: str,
    payload: ActorBindingRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> ActorBindingView:
    reviewer = _membership(session, campaign_id, user.id)
    if reviewer.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    target = session.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    _membership(session, campaign_id, target.id)
    try:
        receipt = await _runtime(request).grant_actor_access(
            campaign_id=campaign_id,
            actor_id=actor_id,
            principal_id=target.principal_id,
            can_control=payload.can_control,
            can_view_private=payload.can_view_private,
            by_principal_id=user.principal_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    item = session.scalar(
        select(ActorBindingProjection).where(
            ActorBindingProjection.campaign_id == campaign_id,
            ActorBindingProjection.actor_id == actor_id,
            ActorBindingProjection.user_id == target.id,
        )
    )
    if item is None:
        item = ActorBindingProjection(
            campaign_id=campaign_id,
            actor_id=actor_id,
            user_id=target.id,
        )
        session.add(item)
    item.can_control = payload.can_control
    item.can_view_private = payload.can_view_private
    item.status = "active" if payload.can_control or payload.can_view_private else "revoked"
    item.mcp_receipt = receipt
    session.commit()
    return ActorBindingView.model_validate(item)
