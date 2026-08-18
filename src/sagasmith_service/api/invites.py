from __future__ import annotations

import secrets
from datetime import UTC, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import (
    AuditEvent,
    CampaignInvite,
    CampaignMembershipProjection,
    CampaignProjection,
    JoinRequest,
    User,
    now_utc,
)
from sagasmith_service.schemas import (
    InviteAcceptRequest,
    InviteCreate,
    InviteCreated,
    InviteView,
    JoinRequestView,
)
from sagasmith_service.security import token_hash

router = APIRouter(tags=["campaign-invites"])


def _campaign_runtime(request: Request, session: DbSession, campaign_id: str) -> Any:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    runtimes = getattr(request.app.state, "game_runtimes", {})
    runtime = runtimes.get(campaign.system_id) if isinstance(runtimes, dict) else None
    if runtime is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported game system")
    return runtime


def _dm(session: DbSession, campaign_id: str, user_id: str) -> CampaignMembershipProjection:
    member = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user_id,
            CampaignMembershipProjection.status == "active",
        )
    )
    if member is None or member.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    return member


@router.post(
    "/api/campaigns/{campaign_id}/invites",
    response_model=InviteCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_invite(
    campaign_id: str,
    payload: InviteCreate,
    user: CurrentUser,
    session: DbSession,
) -> InviteCreated:
    _dm(session, campaign_id, user.id)
    raw_token = secrets.token_urlsafe(32)
    item = CampaignInvite(
        campaign_id=campaign_id,
        token_hash=token_hash(raw_token),
        intended_role="player",
        mode=payload.mode,
        created_by_user_id=user.id,
        expires_at=now_utc() + timedelta(hours=payload.expires_in_hours),
        max_uses=payload.max_uses,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.invite.create",
            subject_type="campaign_invite",
            subject_id=item.id,
            details={"campaign_id": campaign_id, "mode": item.mode},
        )
    )
    session.commit()
    return InviteCreated(invite=InviteView.model_validate(item), token=raw_token)


@router.get("/api/campaigns/{campaign_id}/invites", response_model=list[InviteView])
def list_invites(campaign_id: str, user: CurrentUser, session: DbSession) -> list[InviteView]:
    _dm(session, campaign_id, user.id)
    return [
        InviteView.model_validate(item)
        for item in session.scalars(
            select(CampaignInvite)
            .where(CampaignInvite.campaign_id == campaign_id)
            .order_by(CampaignInvite.created_at.desc())
        ).all()
    ]


@router.delete("/api/campaigns/{campaign_id}/invites/{invite_id}", status_code=204)
def revoke_invite(campaign_id: str, invite_id: str, user: CurrentUser, session: DbSession) -> None:
    _dm(session, campaign_id, user.id)
    item = session.scalar(
        select(CampaignInvite).where(
            CampaignInvite.id == invite_id,
            CampaignInvite.campaign_id == campaign_id,
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    item.revoked_at = now_utc()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.invite.revoke",
            subject_type="campaign_invite",
            subject_id=item.id,
            details={"campaign_id": campaign_id},
        )
    )
    session.commit()


@router.post("/api/invites/accept", response_model=JoinRequestView)
async def accept_invite(
    payload: InviteAcceptRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> JoinRequestView:
    item = session.scalar(
        select(CampaignInvite)
        .where(CampaignInvite.token_hash == token_hash(payload.token))
        .with_for_update()
    )
    now = now_utc()
    expires_at = item.expires_at if item is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        item is None
        or item.revoked_at is not None
        or expires_at is None
        or expires_at <= now
        or item.used_count >= item.max_uses
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite is invalid or expired")
    member = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == item.campaign_id,
            CampaignMembershipProjection.user_id == user.id,
        )
    )
    if member is not None and member.status == "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "already a campaign member")
    join = session.scalar(
        select(JoinRequest).where(
            JoinRequest.campaign_id == item.campaign_id,
            JoinRequest.applicant_user_id == user.id,
            JoinRequest.status == "pending",
        )
    )
    if join is None:
        join = JoinRequest(
            campaign_id=item.campaign_id,
            applicant_user_id=user.id,
            requested_role="player",
            message=payload.message,
        )
        session.add(join)
        session.flush()
    receipt: dict[str, Any] = {}
    if item.mode == "auto_join":
        creator = session.get(User, item.created_by_user_id)
        if creator is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "invite owner no longer exists")
        try:
            receipt = await _campaign_runtime(
                request, session, item.campaign_id
            ).grant_campaign_access(
                campaign_id=item.campaign_id,
                principal_id=user.principal_id,
                role="player",
                by_principal_id=creator.principal_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        if member is None:
            member = CampaignMembershipProjection(
                campaign_id=item.campaign_id,
                user_id=user.id,
                role="player",
            )
            session.add(member)
        member.status = "active"
        member.mcp_receipt = receipt
        join.status = "approved"
        join.reviewed_by_user_id = creator.id
        join.reviewed_at = now
        join.mcp_receipt = receipt
    item.used_count += 1
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.invite.accept",
            subject_type="campaign_invite",
            subject_id=item.id,
            details={"campaign_id": item.campaign_id, "mode": item.mode},
        )
    )
    session.commit()
    return JoinRequestView.model_validate(join)
