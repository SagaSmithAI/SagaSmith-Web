from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.integrations.agent import AgentRuntime
from sagasmith_service.models import (
    AgentConversation,
    AgentIdentity,
    Artifact,
    ArtifactCollaborator,
    ArtifactFavorite,
    ArtifactInstallation,
    ArtifactRelease,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignPackProjection,
    CampaignProjection,
    CommunityPost,
    CommunityReport,
    IdentityCampaignAssignment,
    PrivatePack,
    User,
    now_utc,
)
from sagasmith_service.quota import QuotaExceededError, release, reserve, settle
from sagasmith_service.schemas import (
    ArtifactCreate,
    ArtifactForkCreate,
    ArtifactInstallationView,
    ArtifactInstallRequest,
    ArtifactReleaseCreate,
    ArtifactReleaseView,
    ArtifactUpdate,
    ArtifactView,
    CollaboratorCreate,
    CommunityPostCreate,
    CommunityPostView,
    CommunityReportCreate,
    CommunityReportDecision,
    CommunityReportView,
    ModerationDecision,
)
from sagasmith_service.storage import PrivateStorageError

router = APIRouter(prefix="/api/community", tags=["community"])

PUBLIC_LICENSES = {
    "ARR",
    "CC0-1.0",
    "CC-BY-4.0",
    "CC-BY-SA-4.0",
    "OGL-1.0a",
    "ORC-1.0",
}
FORKABLE_LICENSES = PUBLIC_LICENSES - {"ARR"}
PACK_ARTIFACT_TYPES = {"module", "rule", "character"}


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator required")


def _can_edit(session: DbSession, artifact: Artifact, user_id: str) -> bool:
    if artifact.owner_user_id == user_id:
        return True
    return (
        session.scalar(
            select(ArtifactCollaborator.id).where(
                ArtifactCollaborator.artifact_id == artifact.id,
                ArtifactCollaborator.user_id == user_id,
                ArtifactCollaborator.status == "active",
                ArtifactCollaborator.role == "editor",
            )
        )
        is not None
    )


def _visible_artifact(session: DbSession, artifact_id: str, user: User) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    if artifact.visibility == "private" and not _can_edit(session, artifact, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    if (
        artifact.status not in {"published", "withdrawn"}
        and not _can_edit(session, artifact, user.id)
        and not user.is_admin
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return artifact


def _editable_artifact(session: DbSession, artifact_id: str, user: User) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or not _can_edit(session, artifact, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "editable artifact not found")
    return artifact


def _artifact_view(session: DbSession, artifact: Artifact) -> ArtifactView:
    owner = session.get(User, artifact.owner_user_id)
    favorite_count = (
        session.scalar(
            select(func.count(ArtifactFavorite.id)).where(
                ArtifactFavorite.artifact_id == artifact.id
            )
        )
        or 0
    )
    latest = session.scalar(
        select(ArtifactRelease)
        .where(
            ArtifactRelease.artifact_id == artifact.id,
            ArtifactRelease.status == "published",
        )
        .order_by(ArtifactRelease.published_at.desc())
    )
    return ArtifactView(
        **{
            column: getattr(artifact, column)
            for column in (
                "id",
                "owner_user_id",
                "slug",
                "artifact_type",
                "title",
                "summary",
                "system_id",
                "visibility",
                "status",
                "license_code",
                "rights_attested",
                "source_kind",
                "provenance",
                "tags",
                "forked_from_artifact_id",
                "discussion_enabled",
            )
        },
        owner_display_name=owner.display_name if owner else "Deleted user",
        favorite_count=int(favorite_count),
        latest_release_id=latest.id if latest else None,
        latest_version=latest.version if latest else None,
    )


def _release(session: DbSession, artifact_id: str, release_id: str) -> ArtifactRelease:
    item = session.scalar(
        select(ArtifactRelease).where(
            ArtifactRelease.id == release_id,
            ArtifactRelease.artifact_id == artifact_id,
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release not found")
    return item


def _has_executable_shape(value: Any, path: tuple[str, ...] = ()) -> bool:
    forbidden = {"executable", "entrypoint", "python", "javascript", "script", "binary"}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in forbidden or _has_executable_shape(child, (*path, str(key))):
                return True
    elif isinstance(value, list):
        return any(_has_executable_shape(child, path) for child in value)
    return False


async def _suspend_identity(
    request: Request,
    session: DbSession,
    identity: AgentIdentity,
    moderator: User,
) -> None:
    """Revoke every live MCP grant before closing the service projection."""
    assignments = session.scalars(
        select(IdentityCampaignAssignment).where(
            IdentityCampaignAssignment.identity_id == identity.id,
            IdentityCampaignAssignment.status.in_({"pending", "accepted"}),
        )
    ).all()
    for assignment in assignments:
        receipt: dict[str, Any] = {}
        if assignment.status == "accepted":
            campaign = session.get(CampaignProjection, assignment.campaign_id)
            owner = session.get(User, campaign.owner_user_id) if campaign else None
            if owner is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "cannot revoke Identity grant without its campaign owner projection",
                )
            try:
                receipt = await request.app.state.dnd_runtime.revoke_campaign_access(
                    campaign_id=assignment.campaign_id,
                    principal_id=identity.principal_id,
                    by_principal_id=owner.principal_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        assignment.status = "revoked"
        assignment.active_key = None
        assignment.revoked_at = now_utc()
        assignment.mcp_receipt = {"grant": assignment.mcp_receipt, "revoke": receipt}
        for conversation in session.scalars(
            select(AgentConversation).where(
                AgentConversation.identity_assignment_id == assignment.id,
                AgentConversation.status == "active",
            )
        ):
            conversation.status = "closed"
        session.add(
            AuditEvent(
                actor_user_id=moderator.id,
                action="identity.assignment.moderation_revoke",
                subject_type="identity_assignment",
                subject_id=assignment.id,
                details={"identity_id": identity.id, "campaign_id": assignment.campaign_id},
            )
        )
        # An external MCP revoke cannot participate in the SQL transaction. Persist each
        # completed projection before attempting the next grant so retries converge safely.
        session.commit()
    identity.status = "suspended"


@router.get("/artifacts", response_model=list[ArtifactView])
def list_artifacts(
    user: CurrentUser,
    session: DbSession,
    q: Annotated[str, Query(max_length=200)] = "",
    artifact_type: Annotated[str | None, Query()] = None,
    system_id: Annotated[str | None, Query()] = None,
    mine: bool = False,
    favorites: bool = False,
) -> list[ArtifactView]:
    statement = select(Artifact)
    if mine:
        statement = statement.where(Artifact.owner_user_id == user.id)
    else:
        statement = statement.where(
            or_(
                (Artifact.status == "published") & (Artifact.visibility == "public"),
                Artifact.owner_user_id == user.id,
            )
        )
    if favorites:
        statement = statement.join(ArtifactFavorite).where(ArtifactFavorite.user_id == user.id)
    if artifact_type:
        statement = statement.where(Artifact.artifact_type == artifact_type)
    if system_id:
        statement = statement.where(Artifact.system_id == system_id)
    if q.strip():
        term = f"%{q.strip().casefold()}%"
        statement = statement.where(
            or_(func.lower(Artifact.title).like(term), func.lower(Artifact.summary).like(term))
        )
    items = session.scalars(statement.order_by(Artifact.updated_at.desc()).limit(100)).all()
    return [_artifact_view(session, item) for item in items]


@router.post("/artifacts", response_model=ArtifactView, status_code=status.HTTP_201_CREATED)
def create_artifact(payload: ArtifactCreate, user: CurrentUser, session: DbSession) -> ArtifactView:
    if payload.visibility == "public" and not payload.rights_attested:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "rights attestation required")
    if payload.source_kind == "private_source" and payload.visibility != "private":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "private-source work must remain private"
        )
    if session.scalar(
        select(Artifact.id).where(Artifact.owner_user_id == user.id, Artifact.slug == payload.slug)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "artifact slug already exists")
    item = Artifact(owner_user_id=user.id, **payload.model_dump())
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.artifact.create",
            subject_type="artifact",
            subject_id=item.id,
            details={"artifact_type": item.artifact_type, "visibility": item.visibility},
        )
    )
    session.commit()
    return _artifact_view(session, item)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView)
def get_artifact(artifact_id: str, user: CurrentUser, session: DbSession) -> ArtifactView:
    return _artifact_view(session, _visible_artifact(session, artifact_id, user))


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactView)
def update_artifact(
    artifact_id: str, payload: ArtifactUpdate, user: CurrentUser, session: DbSession
) -> ArtifactView:
    item = _editable_artifact(session, artifact_id, user)
    changes = payload.model_dump(exclude_unset=True)
    protected = {
        "title",
        "summary",
        "tags",
        "visibility",
        "license_code",
        "rights_attested",
        "source_kind",
        "provenance",
    }
    if protected.intersection(changes) and session.scalar(
        select(ArtifactRelease.id).where(
            ArtifactRelease.artifact_id == item.id,
            ArtifactRelease.status.in_({"moderation_pending", "published"}),
        )
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "publication fields are frozen while a release is pending or published",
        )
    owner_only = {
        "visibility",
        "license_code",
        "rights_attested",
        "source_kind",
        "provenance",
    }
    if owner_only.intersection(changes) and item.owner_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the owner may change publication rights and provenance",
        )
    if changes.get("visibility") == "public" and not changes.get(
        "rights_attested", item.rights_attested
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "rights attestation required")
    if (
        changes.get("source_kind", item.source_kind) == "private_source"
        and changes.get("visibility", item.visibility) != "private"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "private-source work must remain private"
        )
    for key, value in changes.items():
        setattr(item, key, value)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.artifact.update",
            subject_type="artifact",
            subject_id=item.id,
            details={"fields": sorted(changes)},
        )
    )
    session.commit()
    return _artifact_view(session, item)


@router.post("/artifacts/{artifact_id}/collaborators", status_code=status.HTTP_201_CREATED)
def add_collaborator(
    artifact_id: str, payload: CollaboratorCreate, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    item = session.get(Artifact, artifact_id)
    if item is None or item.owner_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "owned artifact not found")
    if session.get(User, payload.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    existing = session.scalar(
        select(ArtifactCollaborator).where(
            ArtifactCollaborator.artifact_id == artifact_id,
            ArtifactCollaborator.user_id == payload.user_id,
        )
    )
    if existing is None:
        existing = ArtifactCollaborator(artifact_id=artifact_id, **payload.model_dump())
        session.add(existing)
    else:
        existing.role = payload.role
        existing.status = "active"
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.collaborator.grant",
            subject_type="artifact_collaborator",
            subject_id=existing.id,
            details={"artifact_id": artifact_id, "user_id": payload.user_id},
        )
    )
    session.commit()
    return {"id": existing.id, "role": existing.role, "status": existing.status}


@router.delete(
    "/artifacts/{artifact_id}/collaborators/{collaborator_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_collaborator(
    artifact_id: str,
    collaborator_user_id: str,
    user: CurrentUser,
    session: DbSession,
) -> None:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.owner_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "owned artifact not found")
    item = session.scalar(
        select(ArtifactCollaborator).where(
            ArtifactCollaborator.artifact_id == artifact_id,
            ArtifactCollaborator.user_id == collaborator_user_id,
        )
    )
    if item is None or item.status == "revoked":
        return
    item.status = "revoked"
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.collaborator.revoke",
            subject_type="artifact_collaborator",
            subject_id=item.id,
            details={"artifact_id": artifact_id, "user_id": collaborator_user_id},
        )
    )
    session.commit()


@router.post(
    "/artifacts/{artifact_id}/releases",
    response_model=ArtifactReleaseView,
    status_code=status.HTTP_201_CREATED,
)
def create_release(
    artifact_id: str,
    payload: ArtifactReleaseCreate,
    user: CurrentUser,
    session: DbSession,
) -> ArtifactReleaseView:
    artifact = _editable_artifact(session, artifact_id, user)
    if artifact.artifact_type == "rule" and (
        _has_executable_shape(payload.manifest) or _has_executable_shape(payload.payload)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "executable rule plugins are not accepted"
        )
    pack = None
    if payload.private_pack_id:
        pack = session.scalar(
            select(PrivatePack).where(
                PrivatePack.id == payload.private_pack_id,
                PrivatePack.owner_user_id == artifact.owner_user_id,
            )
        )
        if pack is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "owned private Pack not found")
        valid_pack = (
            (artifact.artifact_type == "module" and pack.kind == "module")
            or (artifact.artifact_type == "rule" and pack.kind in {"addon", "core_rules"})
            or (artifact.artifact_type == "character" and pack.kind == "preset")
        )
        if not valid_pack:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Pack kind does not match artifact type"
            )
    if artifact.artifact_type in PACK_ARTIFACT_TYPES and pack is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "this artifact type requires a current Pack payload",
        )
    if session.scalar(
        select(ArtifactRelease.id).where(
            ArtifactRelease.artifact_id == artifact.id,
            ArtifactRelease.version == payload.version,
        )
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "release version already exists")
    item = ArtifactRelease(artifact_id=artifact.id, **payload.model_dump())
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.release.create",
            subject_type="artifact_release",
            subject_id=item.id,
            details={"artifact_id": artifact.id, "version": item.version},
        )
    )
    session.commit()
    return ArtifactReleaseView.model_validate(item)


@router.get("/artifacts/{artifact_id}/releases", response_model=list[ArtifactReleaseView])
def list_releases(
    artifact_id: str, user: CurrentUser, session: DbSession
) -> list[ArtifactReleaseView]:
    artifact = _visible_artifact(session, artifact_id, user)
    statement = select(ArtifactRelease).where(ArtifactRelease.artifact_id == artifact.id)
    if not _can_edit(session, artifact, user.id) and not user.is_admin:
        statement = statement.where(ArtifactRelease.status == "published")
    return [
        ArtifactReleaseView.model_validate(item)
        for item in session.scalars(statement.order_by(ArtifactRelease.created_at.desc())).all()
    ]


def _review_json(content: str) -> dict[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Agent review did not return strict JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("approved"), bool):
        raise RuntimeError("Agent review is missing approved")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise RuntimeError("Agent review is missing summary")
    findings = value.get("findings", [])
    if not isinstance(findings, list):
        raise RuntimeError("Agent review findings must be a list")
    normalized_findings = []
    for finding in findings[:100]:
        if not isinstance(finding, dict):
            continue
        normalized_findings.append(
            {
                "severity": str(finding.get("severity") or "info")[:24],
                "message": str(finding.get("message") or "")[:1000],
            }
        )
    return {
        "approved": value["approved"],
        "summary": value["summary"][:2000],
        "findings": normalized_findings,
        "reviewer": "hosted-agent",
    }


@router.post(
    "/artifacts/{artifact_id}/releases/{release_id}/agent-review",
    response_model=ArtifactReleaseView,
)
async def agent_review_release(
    artifact_id: str,
    release_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ArtifactReleaseView:
    artifact = _editable_artifact(session, artifact_id, user)
    item = _release(session, artifact_id, release_id)
    if item.status not in {"draft", "agent_reviewed"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "only a draft Release can be Agent-reviewed")
    if item.agent_review.get("idempotency_key") == idempotency_key:
        return ArtifactReleaseView.model_validate(item)
    reservation_quantity = Decimal(request.app.state.settings.agent_reservation_tokens)
    try:
        reservation = reserve(
            session,
            user_id=user.id,
            campaign_id=None,
            metric="llm_tokens",
            quantity=reservation_quantity,
            idempotency_key=f"artifact-review:{user.id}:{idempotency_key}",
            ttl_seconds=300,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    instruction = (
        "Review this SagaSmith community artifact release for semantic completeness, "
        "provenance consistency, privacy/copyright risk, hidden commercial-source material, "
        "and type-specific validity. Do not execute content and do not use MCP tools. "
        'Return strict JSON only: {"approved":boolean,"summary":string,'
        '"findings":[{"severity":string,"message":string}]}.\n'
    )
    prompt = instruction + json.dumps(
        {
            "artifact": {
                "type": artifact.artifact_type,
                "title": artifact.title,
                "summary": artifact.summary,
                "system_id": artifact.system_id,
                "license": artifact.license_code,
                "source_kind": artifact.source_kind,
                "provenance": artifact.provenance,
            },
            "release": {
                "version": item.version,
                "manifest": item.manifest,
                "payload": item.payload,
                "compatibility": item.compatibility,
                "contains_private_source": item.contains_private_source,
            },
        },
        ensure_ascii=False,
    )
    runtime: AgentRuntime = request.app.state.agent_runtime
    try:
        result = await runtime.complete(
            session_id=f"community:{user.id}:{item.id}",
            content=prompt,
            context={
                "campaign_id": "community",
                "principal_id": user.principal_id,
                "campaign_role": "author",
            },
        )
        review = _review_json(result.content)
    except RuntimeError as exc:
        release(session, reservation.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    settle(
        session,
        reservation_id=reservation.id,
        quantity=Decimal(min(result.total_tokens, int(reservation_quantity))),
        idempotency_key=f"artifact-review-settle:{user.id}:{idempotency_key}",
        unit="tokens",
        provider="nanobot",
        model=result.model,
        request_id=result.request_id,
    )
    item.agent_review = {**review, "idempotency_key": idempotency_key}
    item.agent_reviewed_at = now_utc()
    item.status = "agent_reviewed" if review["approved"] else "draft"
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.release.agent_review",
            subject_type="artifact_release",
            subject_id=item.id,
            details={"approved": review["approved"]},
        )
    )
    session.commit()
    return ArtifactReleaseView.model_validate(item)


@router.post(
    "/artifacts/{artifact_id}/releases/{release_id}/submit", response_model=ArtifactReleaseView
)
def submit_release(
    artifact_id: str, release_id: str, user: CurrentUser, session: DbSession
) -> ArtifactReleaseView:
    artifact = _editable_artifact(session, artifact_id, user)
    item = _release(session, artifact_id, release_id)
    failures = []
    if item.status != "agent_reviewed" or not item.agent_review.get("approved"):
        failures.append("approved Agent review required")
    if not artifact.rights_attested:
        failures.append("rights attestation required")
    if artifact.visibility not in {"public", "unlisted"}:
        failures.append("artifact must be public or unlisted")
    if artifact.license_code not in PUBLIC_LICENSES:
        failures.append("unsupported public license")
    if artifact.source_kind == "private_source" or item.contains_private_source:
        failures.append("private-source content cannot be published")
    if artifact.source_kind == "open_licensed" and not artifact.provenance:
        failures.append("open-licensed work requires upstream provenance")
    if failures:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(failures))
    item.status = "moderation_pending"
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.release.submit",
            subject_type="artifact_release",
            subject_id=item.id,
            details={"artifact_id": artifact.id},
        )
    )
    session.commit()
    return ArtifactReleaseView.model_validate(item)


@router.post("/admin/releases/{release_id}/moderate", response_model=ArtifactReleaseView)
def moderate_release(
    release_id: str, payload: ModerationDecision, user: CurrentUser, session: DbSession
) -> ArtifactReleaseView:
    _require_admin(user)
    item = session.get(ArtifactRelease, release_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release not found")
    artifact = session.get(Artifact, item.artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "artifact no longer exists")
    if payload.decision == "approved":
        if item.status != "moderation_pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "release is not pending moderation")
        failures = []
        if not artifact.rights_attested:
            failures.append("rights attestation required")
        if artifact.visibility not in {"public", "unlisted"}:
            failures.append("artifact is no longer publishable")
        if artifact.license_code not in PUBLIC_LICENSES:
            failures.append("unsupported public license")
        if artifact.source_kind == "private_source" or item.contains_private_source:
            failures.append("private-source content cannot be published")
        if not item.agent_review.get("approved"):
            failures.append("approved Agent review required")
        if failures:
            raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(failures))
        item.status = "published"
        item.published_at = now_utc()
        artifact.status = "published"
    elif payload.decision == "rejected":
        if item.status != "moderation_pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "release is not pending moderation")
        item.status = "rejected"
    else:
        if item.status != "published":
            raise HTTPException(
                status.HTTP_409_CONFLICT, "only published releases can be withdrawn"
            )
        item.status = "withdrawn"
        artifact.status = "withdrawn"
    item.moderated_by_user_id = user.id
    item.moderation_notes = payload.notes
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action=f"community.release.{payload.decision}",
            subject_type="artifact_release",
            subject_id=item.id,
            details={"notes": payload.notes},
        )
    )
    session.commit()
    return ArtifactReleaseView.model_validate(item)


@router.get("/admin/releases", response_model=list[ArtifactReleaseView])
def list_moderation_releases(
    user: CurrentUser,
    session: DbSession,
    release_status: str = "moderation_pending",
) -> list[ArtifactReleaseView]:
    _require_admin(user)
    return [
        ArtifactReleaseView.model_validate(item)
        for item in session.scalars(
            select(ArtifactRelease)
            .where(ArtifactRelease.status == release_status)
            .order_by(ArtifactRelease.updated_at)
        ).all()
    ]


@router.put("/artifacts/{artifact_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
def favorite_artifact(artifact_id: str, user: CurrentUser, session: DbSession) -> None:
    _visible_artifact(session, artifact_id, user)
    if (
        session.scalar(
            select(ArtifactFavorite.id).where(
                ArtifactFavorite.artifact_id == artifact_id, ArtifactFavorite.user_id == user.id
            )
        )
        is None
    ):
        session.add(ArtifactFavorite(artifact_id=artifact_id, user_id=user.id))
        session.commit()


@router.delete("/artifacts/{artifact_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
def unfavorite_artifact(artifact_id: str, user: CurrentUser, session: DbSession) -> None:
    item = session.scalar(
        select(ArtifactFavorite).where(
            ArtifactFavorite.artifact_id == artifact_id, ArtifactFavorite.user_id == user.id
        )
    )
    if item:
        session.delete(item)
        session.commit()


@router.post(
    "/artifacts/{artifact_id}/fork",
    response_model=ArtifactView,
    status_code=status.HTTP_201_CREATED,
)
def fork_artifact(
    artifact_id: str, payload: ArtifactForkCreate, user: CurrentUser, session: DbSession
) -> ArtifactView:
    source = _visible_artifact(session, artifact_id, user)
    if source.status != "published" or source.license_code not in FORKABLE_LICENSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "artifact license does not permit platform Fork"
        )
    item = Artifact(
        owner_user_id=user.id,
        slug=payload.slug,
        artifact_type=source.artifact_type,
        title=payload.title,
        summary=source.summary,
        system_id=source.system_id,
        visibility="private",
        status="draft",
        license_code=source.license_code,
        rights_attested=True,
        source_kind="open_licensed",
        provenance={"upstream_artifact_id": source.id, "upstream_license": source.license_code},
        tags=list(source.tags),
        forked_from_artifact_id=source.id,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.artifact.fork",
            subject_type="artifact",
            subject_id=item.id,
            details={"upstream_artifact_id": source.id},
        )
    )
    session.commit()
    return _artifact_view(session, item)


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
    "/releases/{release_id}/install",
    response_model=ArtifactInstallationView,
    status_code=status.HTTP_201_CREATED,
)
async def install_release(
    release_id: str,
    payload: ArtifactInstallRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ArtifactInstallationView:
    item = session.get(ArtifactRelease, release_id)
    if item is None or item.status != "published":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "published release not found")
    artifact = _visible_artifact(session, item.artifact_id, user)
    target_key = f"campaign:{payload.campaign_id}" if payload.campaign_id else "library"
    existing = session.scalar(
        select(ArtifactInstallation).where(
            ArtifactInstallation.installed_by_user_id == user.id,
            ArtifactInstallation.release_id == item.id,
            ArtifactInstallation.target_key == target_key,
        )
    )
    if existing:
        return ArtifactInstallationView.model_validate(existing)
    if artifact.artifact_type in PACK_ARTIFACT_TYPES:
        if not payload.campaign_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "campaign is required for this artifact type"
            )
        _dm(session, payload.campaign_id, user.id)
        if item.content_artifact:
            if artifact.artifact_type != "module":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "compiled content artifacts are currently supported for modules only",
                )
            try:
                receipt = await request.app.state.dnd_runtime.import_content_artifact(
                    campaign_id=payload.campaign_id,
                    artifact=item.content_artifact,
                    principal_id=user.principal_id,
                    idempotency_key=(
                        f"community-module-install:{item.id}:{payload.campaign_id}:"
                        f"{idempotency_key}"
                    ),
                )
                result = receipt.get("result", receipt)
                runtime_ref = str(result.get("module_id") or "")
                if not runtime_ref:
                    raise RuntimeError("D&D MCP returned no imported module id")
                activation = {}
                if payload.activate:
                    activation = await request.app.state.dnd_runtime.activate_content_pack(
                        campaign_id=payload.campaign_id,
                        kind="module",
                        runtime_ref=runtime_ref,
                        pack_id=runtime_ref,
                        version=item.version,
                        principal_id=user.principal_id,
                        idempotency_key=(
                            f"community-module-activate:{item.id}:{payload.campaign_id}:"
                            f"{idempotency_key}"
                        ),
                    )
            except RuntimeError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
            receipt = {"import": receipt, "activation": activation}
            install_kind = "campaign_module"
            campaign_projection_id = None
            installed_status = "activated" if payload.activate else "installed"
        else:
            pack = session.get(PrivatePack, item.private_pack_id)
            if pack is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "release Pack is unavailable")
            if payload.activate and pack.kind == "preset":
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "character presets are imported, not activated",
                )
            projection = session.scalar(
                select(CampaignPackProjection).where(
                    CampaignPackProjection.campaign_id == payload.campaign_id,
                    CampaignPackProjection.private_pack_id == pack.id,
                )
            )
            if projection is None:
                try:
                    exchange_path = request.app.state.private_storage.materialize_for_runtime(
                        pack.storage_key, pack.id
                    )
                except PrivateStorageError as exc:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "artifact storage is temporarily unavailable",
                    ) from exc
                try:
                    receipt = await request.app.state.dnd_runtime.import_content_pack(
                        campaign_id=payload.campaign_id,
                        kind=pack.kind,
                        source_path=str(exchange_path),
                        principal_id=user.principal_id,
                        idempotency_key=(
                            f"community-install:{item.id}:{payload.campaign_id}:{idempotency_key}"
                        ),
                    )
                except RuntimeError as exc:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
                finally:
                    exchange_path.unlink(missing_ok=True)
                result = receipt.get("result", receipt)
                if pack.kind == "module":
                    runtime_ref = str(result.get("module_id") or pack.pack_id)
                elif pack.kind == "addon":
                    runtime_ref = str((result.get("addon") or {}).get("addon_id") or pack.pack_id)
                else:
                    runtime_ref = pack.pack_id
                projection = CampaignPackProjection(
                    campaign_id=payload.campaign_id,
                    private_pack_id=pack.id,
                    imported_by_user_id=user.id,
                    status="imported",
                    runtime_ref=runtime_ref,
                    mcp_receipt=receipt,
                )
                session.add(projection)
                session.flush()
            else:
                receipt = projection.mcp_receipt
                runtime_ref = projection.runtime_ref or pack.pack_id
            if payload.activate and projection.status != "activated":
                try:
                    activation = await request.app.state.dnd_runtime.activate_content_pack(
                        campaign_id=payload.campaign_id,
                        kind=pack.kind,
                        runtime_ref=runtime_ref,
                        pack_id=pack.pack_id,
                        version=pack.version,
                        principal_id=user.principal_id,
                        idempotency_key=f"community-activate:{item.id}:{payload.campaign_id}:{idempotency_key}",
                    )
                except RuntimeError as exc:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
                projection.status = "activated"
                projection.mcp_receipt = {"import": receipt, "activation": activation}
            install_kind = "campaign_pack"
            campaign_projection_id = projection.id
            installed_status = "activated" if projection.status == "activated" else "installed"
    else:
        if payload.campaign_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "this artifact installs to the user's library",
            )
        receipt = {"source_release_id": item.id, "copied_as": "library_reference"}
        runtime_ref = None
        install_kind = "library"
        campaign_projection_id = None
        installed_status = "installed"
    installed = ArtifactInstallation(
        artifact_id=artifact.id,
        release_id=item.id,
        installed_by_user_id=user.id,
        campaign_id=payload.campaign_id,
        target_key=target_key,
        install_kind=install_kind,
        status=installed_status,
        runtime_ref=runtime_ref,
        campaign_pack_projection_id=campaign_projection_id,
        receipt=receipt,
    )
    session.add(installed)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.release.install",
            subject_type="artifact_installation",
            subject_id=installed.id,
            details={
                "artifact_id": artifact.id,
                "release_id": item.id,
                "campaign_id": payload.campaign_id,
            },
        )
    )
    session.commit()
    return ArtifactInstallationView.model_validate(installed)


@router.get("/installations", response_model=list[ArtifactInstallationView])
def list_installations(user: CurrentUser, session: DbSession) -> list[ArtifactInstallationView]:
    return [
        ArtifactInstallationView.model_validate(item)
        for item in session.scalars(
            select(ArtifactInstallation)
            .where(ArtifactInstallation.installed_by_user_id == user.id)
            .order_by(ArtifactInstallation.updated_at.desc())
        ).all()
    ]


def _can_read_owner_post(session: DbSession, post: CommunityPost, user: User) -> bool:
    if post.audience == "public" or post.author_user_id == user.id or user.is_admin:
        return True
    if post.target_type == "artifact":
        artifact = session.get(Artifact, post.target_id)
        if artifact and _can_edit(session, artifact, user.id):
            return True
        return (
            session.scalar(
                select(ArtifactInstallation.id).where(
                    ArtifactInstallation.artifact_id == post.target_id,
                    ArtifactInstallation.installed_by_user_id == user.id,
                )
            )
            is not None
        )
    return False


def _post_view(session: DbSession, item: CommunityPost) -> CommunityPostView:
    author = session.get(User, item.author_user_id)
    return CommunityPostView(
        **{
            column: getattr(item, column)
            for column in (
                "id",
                "author_user_id",
                "target_type",
                "target_id",
                "release_id",
                "parent_id",
                "category",
                "audience",
                "spoiler",
                "body",
                "status",
                "created_at",
            )
        },
        author_display_name=author.display_name if author else "Deleted user",
    )


@router.get("/posts", response_model=list[CommunityPostView])
def list_posts(
    target_type: str, target_id: str, user: CurrentUser, session: DbSession
) -> list[CommunityPostView]:
    if target_type == "artifact":
        artifact = _visible_artifact(session, target_id, user)
        if not artifact.discussion_enabled:
            return []
    items = session.scalars(
        select(CommunityPost)
        .where(
            CommunityPost.target_type == target_type,
            CommunityPost.target_id == target_id,
            CommunityPost.status == "visible",
        )
        .order_by(CommunityPost.created_at)
    ).all()
    return [
        _post_view(session, item) for item in items if _can_read_owner_post(session, item, user)
    ]


@router.post("/posts", response_model=CommunityPostView, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: CommunityPostCreate, user: CurrentUser, session: DbSession
) -> CommunityPostView:
    if payload.target_type == "artifact":
        artifact = _visible_artifact(session, payload.target_id, user)
        if not artifact.discussion_enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, "discussion is disabled")
        if payload.audience == "owners" and not (
            _can_edit(session, artifact, user.id)
            or session.scalar(
                select(ArtifactInstallation.id).where(
                    ArtifactInstallation.artifact_id == artifact.id,
                    ArtifactInstallation.installed_by_user_id == user.id,
                )
            )
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "installation or authorship required for owners audience"
            )
    else:
        from sagasmith_service.models import AgentIdentity

        identity = session.get(AgentIdentity, payload.target_id)
        if identity is None or (
            identity.visibility == "private" and identity.owner_user_id != user.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "identity not found")
    if payload.parent_id:
        parent = session.get(CommunityPost, payload.parent_id)
        if (
            parent is None
            or parent.target_type != payload.target_type
            or parent.target_id != payload.target_id
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid parent post")
    if payload.release_id:
        release_item = session.get(ArtifactRelease, payload.release_id)
        if (
            release_item is None
            or payload.target_type != "artifact"
            or release_item.artifact_id != payload.target_id
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "release does not belong to target"
            )
    item = CommunityPost(author_user_id=user.id, **payload.model_dump())
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.post.create",
            subject_type="community_post",
            subject_id=item.id,
            details={
                "target_type": item.target_type,
                "target_id": item.target_id,
                "spoiler": item.spoiler,
            },
        )
    )
    session.commit()
    return _post_view(session, item)


@router.post("/reports", response_model=CommunityReportView, status_code=status.HTTP_201_CREATED)
def create_report(
    payload: CommunityReportCreate, user: CurrentUser, session: DbSession
) -> CommunityReportView:
    target_exists = False
    if payload.target_type == "artifact":
        target_exists = session.get(Artifact, payload.target_id) is not None
    elif payload.target_type == "post":
        target_exists = session.get(CommunityPost, payload.target_id) is not None
    else:
        from sagasmith_service.models import AgentIdentity

        target_exists = session.get(AgentIdentity, payload.target_id) is not None
    if not target_exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report target not found")
    item = CommunityReport(reporter_user_id=user.id, **payload.model_dump())
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="community.report.create",
            subject_type="community_report",
            subject_id=item.id,
            details={
                "target_type": item.target_type,
                "target_id": item.target_id,
                "reason": item.reason,
            },
        )
    )
    session.commit()
    return CommunityReportView.model_validate(item)


@router.get("/admin/reports", response_model=list[CommunityReportView])
def list_reports(user: CurrentUser, session: DbSession) -> list[CommunityReportView]:
    _require_admin(user)
    return [
        CommunityReportView.model_validate(item)
        for item in session.scalars(
            select(CommunityReport).order_by(CommunityReport.created_at.desc())
        ).all()
    ]


@router.post("/admin/reports/{report_id}/decision", response_model=CommunityReportView)
async def decide_report(
    report_id: str,
    payload: CommunityReportDecision,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> CommunityReportView:
    _require_admin(user)
    item = session.get(CommunityReport, report_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    if payload.status == "resolved" and item.reason in {
        "copyright",
        "commercial_source",
        "privacy",
        "malware",
    }:
        if item.target_type == "artifact":
            artifact = session.get(Artifact, item.target_id)
            if artifact:
                soul_release_ids = session.scalars(
                    select(ArtifactRelease.id).where(ArtifactRelease.artifact_id == artifact.id)
                ).all()
                for identity in session.scalars(
                    select(AgentIdentity).where(
                        AgentIdentity.active_soul_release_id.in_(soul_release_ids)
                    )
                ):
                    await _suspend_identity(request, session, identity, user)
                artifact.status = "withdrawn"
                for release_item in session.scalars(
                    select(ArtifactRelease).where(
                        ArtifactRelease.artifact_id == artifact.id,
                        ArtifactRelease.status == "published",
                    )
                ):
                    release_item.status = "withdrawn"
        elif item.target_type == "post":
            post = session.get(CommunityPost, item.target_id)
            if post:
                post.status = "hidden"
        elif item.target_type == "identity":
            identity = session.get(AgentIdentity, item.target_id)
            if identity:
                await _suspend_identity(request, session, identity, user)
    item.status = payload.status
    item.resolution = payload.resolution
    item.reviewed_by_user_id = user.id
    item.reviewed_at = now_utc()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action=f"community.report.{payload.status}",
            subject_type="community_report",
            subject_id=item.id,
            details={"resolution": payload.resolution},
        )
    )
    session.commit()
    return CommunityReportView.model_validate(item)
