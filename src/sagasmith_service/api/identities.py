from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import func, or_, select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import (
    AgentConversation,
    AgentIdentity,
    Artifact,
    ArtifactRelease,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignProjection,
    IdentityCampaignAssignment,
    IdentityMemoryEntry,
    User,
    new_id,
    now_utc,
)
from sagasmith_service.schemas import (
    AgentIdentityCreate,
    AgentIdentityUpdate,
    AgentIdentityView,
    IdentityAssignmentDecision,
    IdentityAssignmentView,
    IdentityInviteCreate,
    IdentityMemoryUpsert,
    IdentityMemoryView,
)

router = APIRouter(prefix="/api/identities", tags=["identities"])


def _campaign_runtime(request: Request, session: DbSession, campaign_id: str) -> Any:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    runtimes = getattr(request.app.state, "game_runtimes", {})
    runtime = runtimes.get(campaign.system_id) if isinstance(runtimes, dict) else None
    if runtime is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported game system")
    return runtime


def _identity_view(session: DbSession, item: AgentIdentity) -> AgentIdentityView:
    owner = session.get(User, item.owner_user_id)
    return AgentIdentityView(
        **{
            column: getattr(item, column)
            for column in (
                "id",
                "owner_user_id",
                "handle",
                "name",
                "identity_kind",
                "system_id",
                "bio",
                "avatar_url",
                "visibility",
                "status",
                "availability",
                "active_soul_release_id",
                "memory_policy",
                "public_profile",
                "principal_id",
            )
        },
        owner_display_name=owner.display_name if owner else "Deleted user",
    )


def _soul_release(session: DbSession, release_id: str, user_id: str) -> ArtifactRelease:
    release = session.get(ArtifactRelease, release_id)
    artifact = session.get(Artifact, release.artifact_id) if release else None
    if (
        release is None
        or artifact is None
        or artifact.artifact_type != "soul"
        or release.status != "published"
        or (artifact.visibility == "private" and artifact.owner_user_id != user_id)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "published Soul release required"
        )
    return release


def _visible_identity(session: DbSession, identity_id: str, user: User) -> AgentIdentity:
    item = session.get(AgentIdentity, identity_id)
    if item is None or (item.visibility == "private" and item.owner_user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "identity not found")
    return item


def _assignment_access(
    session: DbSession, assignment_id: str, user: User, *, manage: bool = False
) -> tuple[IdentityCampaignAssignment, AgentIdentity, CampaignMembershipProjection | None]:
    assignment = session.get(IdentityCampaignAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assignment not found")
    identity = session.get(AgentIdentity, assignment.identity_id)
    membership = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == assignment.campaign_id,
            CampaignMembershipProjection.user_id == user.id,
            CampaignMembershipProjection.status == "active",
        )
    )
    is_identity_owner = identity is not None and identity.owner_user_id == user.id
    is_campaign_dm = membership is not None and membership.role in {"owner", "dm"}
    if not (is_identity_owner or is_campaign_dm or (not manage and membership is not None)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "assignment access denied")
    if identity is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "identity no longer exists")
    return assignment, identity, membership


@router.get("", response_model=list[AgentIdentityView])
def list_identities(
    user: CurrentUser,
    session: DbSession,
    system_id: str | None = None,
    identity_kind: str | None = None,
    mine: bool = False,
) -> list[AgentIdentityView]:
    statement = select(AgentIdentity)
    if mine:
        statement = statement.where(AgentIdentity.owner_user_id == user.id)
    else:
        statement = statement.where(
            or_(
                (AgentIdentity.visibility == "public") & (AgentIdentity.status == "active"),
                AgentIdentity.owner_user_id == user.id,
            )
        )
    if system_id:
        statement = statement.where(AgentIdentity.system_id == system_id)
    if identity_kind:
        statement = statement.where(AgentIdentity.identity_kind == identity_kind)
    return [
        _identity_view(session, item)
        for item in session.scalars(statement.order_by(AgentIdentity.updated_at.desc())).all()
    ]


@router.post("", response_model=AgentIdentityView, status_code=status.HTTP_201_CREATED)
def create_identity(
    payload: AgentIdentityCreate, user: CurrentUser, session: DbSession
) -> AgentIdentityView:
    soul = _soul_release(session, payload.active_soul_release_id, user.id)
    soul_artifact = session.get(Artifact, soul.artifact_id)
    if payload.identity_kind == "dm" and payload.system_id != "dnd5e":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "DM Identity requires dnd5e")
    if payload.identity_kind == "keeper" and payload.system_id != "coc7e":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Keeper Identity requires coc7e")
    if soul_artifact and soul_artifact.system_id not in {payload.system_id, "system-neutral"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Soul system is incompatible")
    if session.scalar(select(AgentIdentity.id).where(AgentIdentity.handle == payload.handle)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Identity handle already exists")
    item = AgentIdentity(owner_user_id=user.id, status="active", **payload.model_dump())
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="identity.create",
            subject_type="agent_identity",
            subject_id=item.id,
            details={"identity_kind": item.identity_kind, "system_id": item.system_id},
        )
    )
    session.commit()
    return _identity_view(session, item)


@router.get("/{identity_id}", response_model=AgentIdentityView)
def get_identity(identity_id: str, user: CurrentUser, session: DbSession) -> AgentIdentityView:
    return _identity_view(session, _visible_identity(session, identity_id, user))


@router.patch("/{identity_id}", response_model=AgentIdentityView)
def update_identity(
    identity_id: str,
    payload: AgentIdentityUpdate,
    user: CurrentUser,
    session: DbSession,
) -> AgentIdentityView:
    item = session.get(AgentIdentity, identity_id)
    if item is None or item.owner_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "owned identity not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("active_soul_release_id"):
        soul = _soul_release(session, changes["active_soul_release_id"], user.id)
        soul_artifact = session.get(Artifact, soul.artifact_id)
        if soul_artifact and soul_artifact.system_id not in {
            item.system_id,
            "system-neutral",
        }:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Soul system is incompatible"
            )
    for key, value in changes.items():
        setattr(item, key, value)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="identity.update",
            subject_type="agent_identity",
            subject_id=item.id,
            details={"fields": sorted(changes)},
        )
    )
    session.commit()
    return _identity_view(session, item)


@router.post(
    "/campaigns/{campaign_id}/invitations",
    response_model=IdentityAssignmentView,
    status_code=status.HTTP_201_CREATED,
)
def invite_identity(
    campaign_id: str,
    payload: IdentityInviteCreate,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> IdentityAssignmentView:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None or campaign.owner_user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign owner required")
    identity = _visible_identity(session, payload.identity_id, user)
    if identity.status != "active" or identity.availability == "unavailable":
        raise HTTPException(status.HTTP_409_CONFLICT, "identity is not accepting invitations")
    _soul_release(session, identity.active_soul_release_id, identity.owner_user_id)
    if campaign.system_id != identity.system_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "identity system is incompatible"
        )
    expected_identity_kind = "dm" if campaign.system_id == "dnd5e" else "keeper"
    if identity.identity_kind != expected_identity_kind:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "identity kind is incompatible with the campaign host role",
        )
    payer_id = payload.quota_payer_user_id or user.id
    if payer_id not in {user.id, identity.owner_user_id}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "quota payer must be the campaign owner or Identity owner",
        )
    existing = session.scalar(
        select(IdentityCampaignAssignment).where(
            IdentityCampaignAssignment.invited_by_user_id == user.id,
            IdentityCampaignAssignment.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return IdentityAssignmentView.model_validate(existing)
    if session.scalar(
        select(IdentityCampaignAssignment.id).where(
            IdentityCampaignAssignment.identity_id == identity.id,
            IdentityCampaignAssignment.campaign_id == campaign.id,
            IdentityCampaignAssignment.status.in_({"pending", "accepted"}),
        )
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Identity already has an active campaign invitation"
        )
    assignment_id = new_id()
    item = IdentityCampaignAssignment(
        id=assignment_id,
        identity_id=identity.id,
        active_key=f"{campaign.id}:{identity.id}",
        campaign_id=campaign.id,
        soul_release_id=identity.active_soul_release_id,
        role="dm",
        invited_by_user_id=user.id,
        quota_payer_user_id=payer_id,
        idempotency_key=idempotency_key,
        memory_namespace=(
            f"campaign:{campaign.id}:identity:{identity.id}:assignment:{assignment_id}"
        ),
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="identity.assignment.invite",
            subject_type="identity_assignment",
            subject_id=item.id,
            details={"campaign_id": campaign.id, "identity_id": identity.id, "role": "dm"},
        )
    )
    session.commit()
    return IdentityAssignmentView.model_validate(item)


@router.get("/assignments/mine", response_model=list[IdentityAssignmentView])
def list_assignments(user: CurrentUser, session: DbSession) -> list[IdentityAssignmentView]:
    identity_ids = select(AgentIdentity.id).where(AgentIdentity.owner_user_id == user.id)
    campaign_ids = select(CampaignMembershipProjection.campaign_id).where(
        CampaignMembershipProjection.user_id == user.id,
        CampaignMembershipProjection.status == "active",
    )
    items = session.scalars(
        select(IdentityCampaignAssignment)
        .where(
            or_(
                IdentityCampaignAssignment.identity_id.in_(identity_ids),
                IdentityCampaignAssignment.campaign_id.in_(campaign_ids),
            )
        )
        .order_by(IdentityCampaignAssignment.updated_at.desc())
    ).all()
    return [IdentityAssignmentView.model_validate(item) for item in items]


@router.post("/assignments/{assignment_id}/decision", response_model=IdentityAssignmentView)
async def decide_assignment(
    assignment_id: str,
    payload: IdentityAssignmentDecision,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> IdentityAssignmentView:
    item = session.scalar(
        select(IdentityCampaignAssignment)
        .where(IdentityCampaignAssignment.id == assignment_id)
        .with_for_update()
    )
    identity = session.get(AgentIdentity, item.identity_id) if item else None
    if item is None or identity is None or identity.owner_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "owned invitation not found")
    if item.status != "pending":
        if item.status == payload.decision:
            return IdentityAssignmentView.model_validate(item)
        raise HTTPException(status.HTTP_409_CONFLICT, "invitation already decided")
    receipt = {}
    if payload.decision == "accepted":
        inviter = session.get(User, item.invited_by_user_id)
        if inviter is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "campaign owner no longer exists")
        try:
            receipt = await _campaign_runtime(
                request, session, item.campaign_id
            ).grant_campaign_access(
                campaign_id=item.campaign_id,
                principal_id=identity.principal_id,
                role="dm",
                by_principal_id=inviter.principal_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    item.status = payload.decision
    if payload.decision == "rejected":
        item.active_key = None
    item.responded_at = now_utc()
    item.mcp_receipt = receipt
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action=f"identity.assignment.{payload.decision}",
            subject_type="identity_assignment",
            subject_id=item.id,
            details={"campaign_id": item.campaign_id, "identity_id": item.identity_id},
        )
    )
    session.commit()
    return IdentityAssignmentView.model_validate(item)


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_assignment(
    assignment_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> None:
    item, identity, membership = _assignment_access(session, assignment_id, user, manage=True)
    if item.status == "revoked":
        return
    if membership is not None and membership.role != "owner" and identity.owner_user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign or Identity owner required")
    campaign = session.get(CampaignProjection, item.campaign_id)
    caller = user
    if campaign and campaign.owner_user_id != user.id:
        caller = session.get(User, campaign.owner_user_id) or user
    if item.status == "accepted":
        try:
            receipt = await _campaign_runtime(
                request, session, item.campaign_id
            ).revoke_campaign_access(
                campaign_id=item.campaign_id,
                principal_id=identity.principal_id,
                by_principal_id=caller.principal_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        item.mcp_receipt = {"grant": item.mcp_receipt, "revoke": receipt}
    item.status = "revoked"
    item.active_key = None
    item.revoked_at = now_utc()
    for conversation in session.scalars(
        select(AgentConversation).where(
            AgentConversation.identity_assignment_id == item.id,
            AgentConversation.status == "active",
        )
    ):
        conversation.status = "closed"
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="identity.assignment.revoke",
            subject_type="identity_assignment",
            subject_id=item.id,
            details={"campaign_id": item.campaign_id, "identity_id": item.identity_id},
        )
    )
    session.commit()


@router.get("/assignments/{assignment_id}/memory", response_model=list[IdentityMemoryView])
def list_identity_memory(
    assignment_id: str, user: CurrentUser, session: DbSession
) -> list[IdentityMemoryView]:
    _assignment_access(session, assignment_id, user, manage=True)
    return [
        IdentityMemoryView.model_validate(item)
        for item in session.scalars(
            select(IdentityMemoryEntry)
            .where(IdentityMemoryEntry.assignment_id == assignment_id)
            .order_by(IdentityMemoryEntry.memory_key)
        ).all()
    ]


@router.put("/assignments/{assignment_id}/memory/{memory_key}", response_model=IdentityMemoryView)
def upsert_identity_memory(
    assignment_id: str,
    memory_key: str,
    payload: IdentityMemoryUpsert,
    user: CurrentUser,
    session: DbSession,
) -> IdentityMemoryView:
    assignment, _identity, _membership = _assignment_access(
        session, assignment_id, user, manage=True
    )
    if assignment.status != "accepted":
        raise HTTPException(status.HTTP_409_CONFLICT, "accepted assignment required")
    if (
        not memory_key
        or len(memory_key) > 100
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for char in memory_key.casefold()
        )
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid memory key")
    item = session.scalar(
        select(IdentityMemoryEntry)
        .where(
            IdentityMemoryEntry.assignment_id == assignment_id,
            IdentityMemoryEntry.memory_key == memory_key,
        )
        .with_for_update()
    )
    if item is None:
        memory_count = session.scalar(
            select(func.count(IdentityMemoryEntry.id)).where(
                IdentityMemoryEntry.assignment_id == assignment_id
            )
        )
        if (memory_count or 0) >= 100:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Identity campaign memory entry limit reached"
            )
        if payload.expected_revision is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "memory entry does not exist")
        item = IdentityMemoryEntry(
            assignment_id=assignment_id,
            memory_key=memory_key,
            content=payload.content,
            audience=payload.audience,
            source=payload.source,
        )
        session.add(item)
    else:
        if payload.expected_revision != item.revision:
            raise HTTPException(status.HTTP_409_CONFLICT, "stale memory revision")
        item.content = payload.content
        item.audience = payload.audience
        item.source = payload.source
        item.revision += 1
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="identity.memory.write",
            subject_type="identity_memory",
            subject_id=item.id,
            details={
                "assignment_id": assignment_id,
                "memory_key": memory_key,
                "revision": item.revision,
            },
        )
    )
    session.commit()
    return IdentityMemoryView.model_validate(item)
