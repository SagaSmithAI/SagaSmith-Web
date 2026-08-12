from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import (
    Artifact,
    ArtifactRelease,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignProjection,
    ModuleDecision,
    ModuleInstallation,
    ModuleProject,
    ModuleRun,
    ModuleSource,
    UserNotification,
    now_utc,
)
from sagasmith_service.schemas import (
    ArtifactReleaseView,
    ModuleFinalizeRequest,
    ModuleInstallationView,
    ModuleInstallRequest,
    ModuleOutlineDecision,
    ModuleProjectCreate,
    ModuleProjectView,
    ModulePublishRequest,
    ModuleRunRequest,
    ModuleRunView,
    ModuleSourceView,
    NotificationView,
)

router = APIRouter(prefix="/api/modules", tags=["module-studio"])
NOTIFICATION_ROUTER = APIRouter(prefix="/api/notifications", tags=["notifications"])
RUN_TYPES = {"outline", "generate", "review", "revise", "finalize", "install"}
ACTIVE_RUN_STATUSES = {"queued", "running"}
SOURCE_MEDIA = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


def _project_specification(payload: ModuleProjectCreate) -> dict[str, Any]:
    return {
        "module_shape": payload.module_shape,
        "starting_level": payload.starting_level,
        "ending_level": payload.ending_level,
        "party_size": payload.party_size,
        "session_hours": payload.session_hours,
        "advancement_mode": payload.advancement_mode,
        "tone": payload.tone,
        "safety": payload.safety,
    }


def _project(session: DbSession, project_id: str, user_id: str) -> ModuleProject:
    item = session.scalar(
        select(ModuleProject).where(
            ModuleProject.id == project_id,
            ModuleProject.owner_user_id == user_id,
            ModuleProject.archived_at.is_(None),
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Module project not found")
    return item


def _campaign_id(receipt: dict[str, Any]) -> str:
    for candidate in [receipt, receipt.get("result", {}), receipt.get("campaign", {})]:
        if isinstance(candidate, dict) and (candidate.get("id") or candidate.get("campaign_id")):
            return str(candidate.get("id") or candidate.get("campaign_id"))
    raise RuntimeError("D&D MCP campaign receipt has no campaign id")


def _input_hash(run_type: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([run_type, payload], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _enqueue(
    session: DbSession,
    *,
    project: ModuleProject,
    user_id: str,
    run_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> ModuleRun:
    if run_type not in RUN_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported task type")
    existing = session.scalar(
        select(ModuleRun).where(
            ModuleRun.requested_by_user_id == user_id,
            ModuleRun.idempotency_key == idempotency_key,
        )
    )
    digest = _input_hash(run_type, payload)
    if existing is not None:
        if (
            existing.project_id != project.id
            or existing.run_type != run_type
            or existing.input_hash != digest
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key payload changed")
        return existing
    active = session.scalar(
        select(ModuleRun.id).where(
            ModuleRun.project_id == project.id,
            ModuleRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This project already has an active task")
    item = ModuleRun(
        project_id=project.id,
        requested_by_user_id=user_id,
        run_type=run_type,
        idempotency_key=idempotency_key,
        input_hash=digest,
        input_payload=payload,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user_id,
            action=f"module.run.{run_type}.enqueue",
            subject_type="module_run",
            subject_id=item.id,
            details={"project_id": project.id},
        )
    )
    return item


def _require_transition(project: ModuleProject, allowed: set[str], action: str) -> None:
    if project.status not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot {action} while project status is {project.status}",
        )


@router.post("", response_model=ModuleProjectView, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ModuleProjectCreate,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleProjectView:
    existing = session.scalar(
        select(ModuleProject).where(
            ModuleProject.owner_user_id == user.id,
            ModuleProject.slug == payload.slug,
        )
    )
    if existing is not None:
        if (
            existing.title != payload.title
            or existing.brief != payload.brief
            or existing.edition != payload.edition
            or existing.locale != payload.locale
            or existing.version != payload.version
            or existing.budget_tokens != payload.budget_tokens
            or existing.specification != _project_specification(payload)
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Module project slug already has different input"
            )
        return ModuleProjectView.model_validate(existing)
    try:
        receipt = await request.app.state.dnd_runtime.create_campaign(
            name=f"Authoring {payload.slug} {user.id[:8]}",
            description="SagaSmith Module Studio authoring workspace",
            edition=payload.edition,
            locale=payload.locale,
            advancement_mode=payload.advancement_mode,
            principal_id=user.principal_id,
            idempotency_key=f"service:module-project:{user.id}:{idempotency_key}",
        )
        campaign_id = _campaign_id(receipt)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    campaign = CampaignProjection(
        id=campaign_id,
        name=f"Authoring · {payload.title}",
        owner_user_id=user.id,
        visibility="private",
        purpose="authoring",
        mcp_receipt={**receipt, "service_idempotency_key": idempotency_key},
    )
    session.add(campaign)
    session.flush()
    session.add(
        CampaignMembershipProjection(
            campaign_id=campaign_id,
            user_id=user.id,
            role="owner",
            mcp_receipt=receipt,
        )
    )
    item = ModuleProject(
        owner_user_id=user.id,
        authoring_campaign_id=campaign_id,
        slug=payload.slug,
        title=payload.title,
        brief=payload.brief,
        edition=payload.edition,
        locale=payload.locale,
        version=payload.version,
        budget_tokens=payload.budget_tokens,
        specification=_project_specification(payload),
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="module.project.create",
            subject_type="module_project",
            subject_id=item.id,
            details={"authoring_campaign_id": campaign_id},
        )
    )
    session.commit()
    return ModuleProjectView.model_validate(item)


@router.get("", response_model=list[ModuleProjectView])
def list_projects(user: CurrentUser, session: DbSession) -> list[ModuleProjectView]:
    items = session.scalars(
        select(ModuleProject)
        .where(ModuleProject.owner_user_id == user.id, ModuleProject.archived_at.is_(None))
        .order_by(ModuleProject.updated_at.desc())
    ).all()
    return [ModuleProjectView.model_validate(item) for item in items]


@router.get("/{project_id}", response_model=ModuleProjectView)
def get_project(project_id: str, user: CurrentUser, session: DbSession) -> ModuleProjectView:
    return ModuleProjectView.model_validate(_project(session, project_id, user.id))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_project(project_id: str, user: CurrentUser, session: DbSession) -> None:
    item = _project(session, project_id, user.id)
    if session.scalar(
        select(ModuleRun.id).where(
            ModuleRun.project_id == item.id, ModuleRun.status.in_(ACTIVE_RUN_STATUSES)
        )
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Cancel the active task before archiving")
    item.archived_at = now_utc()
    item.status = "archived"
    session.commit()


@router.post("/{project_id}/sources", response_model=ModuleSourceView, status_code=201)
def upload_source(
    project_id: str,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    source: Annotated[UploadFile, File()],
    rights_basis: Annotated[str, Form(pattern="^(original|open_licensed|reference_only)$")],
    license_code: Annotated[str, Form(min_length=2, max_length=64)] = "ARR",
    attribution: Annotated[str, Form(max_length=2000)] = "",
) -> ModuleSourceView:
    project = _project(session, project_id, user.id)
    _require_transition(
        project, {"idea", "outline_ready", "draft_review", "failed", "canceled"}, "upload a source"
    )
    suffix = Path(source.filename or "").suffix.casefold()
    if suffix not in SOURCE_MEDIA:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Use PDF, Markdown, or text")
    generation = (
        int(
            session.scalar(
                select(ModuleSource.generation)
                .where(ModuleSource.project_id == project.id)
                .order_by(ModuleSource.generation.desc())
                .limit(1)
            )
            or 0
        )
        + 1
    )
    item_id = hashlib.sha256(f"{project.id}:{generation}:{source.filename}".encode()).hexdigest()[
        :32
    ]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source.filename or "source").name)
    key = f"modules/{user.id}/{project.id}/{item_id}-{safe_name}"
    try:
        digest, size = request.app.state.private_storage.put(
            key,
            source.file,
            max_bytes=request.app.state.settings.max_module_source_bytes,
            content_type=SOURCE_MEDIA[suffix],
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    item = ModuleSource(
        id=item_id,
        project_id=project.id,
        generation=generation,
        source_type="upload",
        name=safe_name,
        storage_key=key,
        sha256=digest,
        size_bytes=size,
        media_type=SOURCE_MEDIA[suffix],
        rights_basis=rights_basis,
        license_code=license_code,
        attribution=attribution,
        public_eligible=rights_basis in {"original", "open_licensed"},
    )
    session.add(item)
    project.current_source_id = item.id
    project.status = "idea"
    project.mcp_job_id = None
    project.mcp_module_id = None
    project.review = {}
    session.commit()
    return ModuleSourceView.model_validate(item)


@router.get("/{project_id}/sources", response_model=list[ModuleSourceView])
def list_sources(project_id: str, user: CurrentUser, session: DbSession) -> list[ModuleSourceView]:
    project = _project(session, project_id, user.id)
    return [
        ModuleSourceView.model_validate(item)
        for item in session.scalars(
            select(ModuleSource)
            .where(ModuleSource.project_id == project.id)
            .order_by(ModuleSource.generation.desc())
        ).all()
    ]


def _queue_action(
    project_id: str,
    run_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    user: Any,
    session: DbSession,
) -> ModuleRunView:
    project = session.scalar(
        select(ModuleProject)
        .where(
            ModuleProject.id == project_id,
            ModuleProject.owner_user_id == user.id,
            ModuleProject.archived_at.is_(None),
        )
        .with_for_update()
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Module project not found")
    allowed = {
        "outline": {"idea", "outline_ready", "failed", "canceled"},
        "generate": {"outline_ready", "failed", "canceled"},
        "review": {"draft_review", "failed", "canceled"},
        "revise": {"draft_review", "ready_to_finalize", "compiled", "failed", "canceled"},
        "finalize": {"ready_to_finalize", "failed"},
        "install": {"compiled"},
    }[run_type]
    _require_transition(project, allowed, run_type)
    item = _enqueue(
        session,
        project=project,
        user_id=user.id,
        run_type=run_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    session.commit()
    return ModuleRunView.model_validate(item)


@router.post("/{project_id}/outline", response_model=ModuleRunView, status_code=202)
def create_outline(
    project_id: str,
    payload: ModuleRunRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    return _queue_action(
        project_id, "outline", payload.model_dump(), idempotency_key, user, session
    )


@router.post("/{project_id}/outline-decision", response_model=ModuleProjectView)
def decide_outline(
    project_id: str,
    payload: ModuleOutlineDecision,
    user: CurrentUser,
    session: DbSession,
) -> ModuleProjectView:
    project = _project(session, project_id, user.id)
    _require_transition(project, {"outline_ready"}, "decide the outline")
    session.add(
        ModuleDecision(
            project_id=project.id,
            actor_user_id=user.id,
            decision_type="outline_approved" if payload.approved else "outline_rejected",
            project_revision=project.outline_revision,
            payload=payload.model_dump(),
        )
    )
    if payload.approved:
        project.status = "outline_ready"
        project.specification = {**project.specification, "outline_approved": True}
    else:
        project.status = "idea"
        project.specification = {
            **project.specification,
            "outline_approved": False,
            "outline_feedback": payload.feedback,
        }
    session.commit()
    return ModuleProjectView.model_validate(project)


@router.post("/{project_id}/generate", response_model=ModuleRunView, status_code=202)
def generate_module(
    project_id: str,
    payload: ModuleRunRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    if not project.specification.get("outline_approved"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Approve the outline before generation")
    return _queue_action(
        project_id, "generate", payload.model_dump(), idempotency_key, user, session
    )


@router.post("/{project_id}/review", response_model=ModuleRunView, status_code=202)
def review_module(
    project_id: str,
    payload: ModuleRunRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    return _queue_action(project_id, "review", payload.model_dump(), idempotency_key, user, session)


@router.post("/{project_id}/revise", response_model=ModuleRunView, status_code=202)
def revise_module(
    project_id: str,
    payload: ModuleRunRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    if project.status == "compiled" and not payload.version:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A new version is required when revising a compiled module",
        )
    if payload.version == project.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "New version must differ from current version"
        )
    return _queue_action(project_id, "revise", payload.model_dump(), idempotency_key, user, session)


@router.post("/{project_id}/finalize", response_model=ModuleRunView, status_code=202)
def finalize_module(
    project_id: str,
    payload: ModuleFinalizeRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    if not payload.confirmed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Finalization must be confirmed")
    return _queue_action(
        project_id, "finalize", payload.model_dump(), idempotency_key, user, session
    )


@router.post("/{project_id}/install", response_model=ModuleRunView, status_code=202)
def install_module(
    project_id: str,
    payload: ModuleInstallRequest,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    membership = session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == payload.campaign_id,
            CampaignMembershipProjection.user_id == user.id,
            CampaignMembershipProjection.role.in_({"owner", "dm"}),
            CampaignMembershipProjection.status == "active",
        )
    )
    campaign = session.get(CampaignProjection, payload.campaign_id)
    if membership is None or campaign is None or campaign.purpose != "play":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM access to a play campaign is required")
    return _queue_action(
        project.id, "install", payload.model_dump(), idempotency_key, user, session
    )


@router.get("/{project_id}/installations", response_model=list[ModuleInstallationView])
def list_installations(
    project_id: str, user: CurrentUser, session: DbSession
) -> list[ModuleInstallationView]:
    project = _project(session, project_id, user.id)
    return [
        ModuleInstallationView.model_validate(item)
        for item in session.scalars(
            select(ModuleInstallation)
            .where(ModuleInstallation.project_id == project.id)
            .order_by(ModuleInstallation.updated_at.desc())
        ).all()
    ]


@router.post("/{project_id}/publish", response_model=ArtifactReleaseView, status_code=201)
def publish_module(
    project_id: str,
    payload: ModulePublishRequest,
    user: CurrentUser,
    session: DbSession,
) -> ArtifactReleaseView:
    project = _project(session, project_id, user.id)
    _require_transition(project, {"compiled"}, "publish")
    if not project.final_artifact or not project.review.get("approved"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Compiled approved module required")
    source = (
        session.get(ModuleSource, project.current_source_id) if project.current_source_id else None
    )
    if source is None or not source.public_eligible or source.rights_basis == "reference_only":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Current source is not publishable"
        )
    required_source_kind = "open_licensed" if source.rights_basis == "open_licensed" else "original"
    if payload.source_kind != required_source_kind:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Publication source_kind must be {required_source_kind}",
        )
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.owner_user_id == user.id,
            Artifact.slug == project.slug,
        )
    )
    if artifact is None:
        artifact = Artifact(
            owner_user_id=user.id,
            slug=project.slug,
            artifact_type="module",
            title=project.title,
            summary=payload.summary or str(project.review.get("summary") or ""),
            system_id="dnd5e",
            visibility=payload.visibility,
            status="draft",
            license_code=payload.license_code,
            rights_attested=True,
            source_kind=payload.source_kind,
            provenance=payload.provenance,
            tags=payload.tags,
        )
        session.add(artifact)
        session.flush()
    prior_release = session.scalar(
        select(ArtifactRelease).where(
            ArtifactRelease.artifact_id == artifact.id,
            ArtifactRelease.version == project.version,
        )
    )
    if prior_release is not None:
        if prior_release.module_project_id != project.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "This module version is already published"
            )
        return ArtifactReleaseView.model_validate(prior_release)
    release = ArtifactRelease(
        artifact_id=artifact.id,
        version=project.version,
        status="moderation_pending",
        changelog=payload.changelog,
        manifest={"module_project_id": project.id, "system_id": "dnd5e"},
        payload={"summary": project.finalization.get("summary", {})},
        compatibility={"system_id": "dnd5e", "edition": project.edition},
        module_project_id=project.id,
        content_artifact=project.final_artifact,
        content_checksum=project.final_checksum,
        contains_private_source=False,
        agent_review={**project.review, "module_finalization": project.finalization},
        agent_reviewed_at=now_utc(),
    )
    session.add(release)
    session.flush()
    project.published_release_id = release.id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="module.publish.submit",
            subject_type="artifact_release",
            subject_id=release.id,
            details={"project_id": project.id, "version": project.version},
        )
    )
    session.commit()
    return ArtifactReleaseView.model_validate(release)


@router.get("/{project_id}/runs", response_model=list[ModuleRunView])
def list_runs(project_id: str, user: CurrentUser, session: DbSession) -> list[ModuleRunView]:
    project = _project(session, project_id, user.id)
    return [
        ModuleRunView.model_validate(item)
        for item in session.scalars(
            select(ModuleRun)
            .where(ModuleRun.project_id == project.id)
            .order_by(ModuleRun.created_at.desc())
        ).all()
    ]


@router.get("/{project_id}/runs/{run_id}", response_model=ModuleRunView)
def get_run(project_id: str, run_id: str, user: CurrentUser, session: DbSession) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    item = session.scalar(
        select(ModuleRun).where(ModuleRun.id == run_id, ModuleRun.project_id == project.id)
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Module task not found")
    return ModuleRunView.model_validate(item)


@router.post("/{project_id}/runs/{run_id}/cancel", response_model=ModuleRunView)
def cancel_run(
    project_id: str, run_id: str, user: CurrentUser, session: DbSession
) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    item = session.scalar(
        select(ModuleRun).where(ModuleRun.id == run_id, ModuleRun.project_id == project.id)
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Module task not found")
    if item.status == "queued":
        item.status = "canceled"
        item.completed_at = now_utc()
        project.status = "canceled"
    elif item.status == "running":
        item.cancel_requested = True
        project.cancel_requested = True
    session.commit()
    return ModuleRunView.model_validate(item)


@router.post("/{project_id}/runs/{run_id}/retry", response_model=ModuleRunView, status_code=202)
def retry_run(
    project_id: str,
    run_id: str,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> ModuleRunView:
    project = _project(session, project_id, user.id)
    source = session.scalar(
        select(ModuleRun).where(ModuleRun.id == run_id, ModuleRun.project_id == project.id)
    )
    if source is None or source.status not in {"failed", "canceled"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only failed or canceled tasks can be retried"
        )
    item = _enqueue(
        session,
        project=project,
        user_id=user.id,
        run_type=source.run_type,
        payload=source.input_payload,
        idempotency_key=idempotency_key,
    )
    session.commit()
    return ModuleRunView.model_validate(item)


@router.get("/{project_id}/events")
async def project_events(project_id: str, request: Request, user: CurrentUser):
    factory = request.app.state.session_factory
    with factory() as session:
        _project(session, project_id, user.id)

    async def stream():
        last = ""
        for _ in range(600):
            if await request.is_disconnected():
                return
            with factory() as session:
                project = _project(session, project_id, user.id)
                run = session.scalar(
                    select(ModuleRun)
                    .where(ModuleRun.project_id == project.id)
                    .order_by(ModuleRun.created_at.desc())
                    .limit(1)
                )
                payload = {
                    "project": ModuleProjectView.model_validate(project).model_dump(mode="json"),
                    "run": ModuleRunView.model_validate(run).model_dump(mode="json")
                    if run
                    else None,
                }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if encoded != last:
                yield f"event: module\ndata: {encoded}\n\n"
                last = encoded
            else:
                yield ": keep-alive\n\n"
            if run and run.status in {"succeeded", "failed", "canceled"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@NOTIFICATION_ROUTER.get("", response_model=list[NotificationView])
def list_notifications(user: CurrentUser, session: DbSession) -> list[NotificationView]:
    return [
        NotificationView.model_validate(item)
        for item in session.scalars(
            select(UserNotification)
            .where(UserNotification.user_id == user.id)
            .order_by(UserNotification.created_at.desc())
            .limit(100)
        ).all()
    ]


@NOTIFICATION_ROUTER.post("/{notification_id}/read", response_model=NotificationView)
def read_notification(
    notification_id: str, user: CurrentUser, session: DbSession
) -> NotificationView:
    item = session.scalar(
        select(UserNotification).where(
            UserNotification.id == notification_id,
            UserNotification.user_id == user.id,
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    item.read_at = item.read_at or now_utc()
    session.commit()
    return NotificationView.model_validate(item)
