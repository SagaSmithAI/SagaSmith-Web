from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import (
    AuditEvent,
    CampaignMembershipProjection,
    CampaignPackProjection,
    PrivatePack,
)
from sagasmith_service.schemas import CampaignPackView, PrivatePackView
from sagasmith_service.storage import LocalPrivateStorage

router = APIRouter(prefix="/api/packs", tags=["private-packs"])


def _dm_membership(
    session: DbSession, campaign_id: str, user_id: str
) -> CampaignMembershipProjection:
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


@router.post("", response_model=PrivatePackView, status_code=status.HTTP_201_CREATED)
def upload_private_pack(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    pack_id: Annotated[str, Form(min_length=1, max_length=160)],
    version: Annotated[str, Form(min_length=1, max_length=80)],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    kind: Annotated[str, Form(pattern="^(module|addon|core_rules|preset)$")],
    rights_attested: Annotated[bool, Form()],
    archive: Annotated[UploadFile, File()],
) -> PrivatePackView:
    if not rights_attested:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "uploader must attest they may store and use this Pack privately",
        )
    if session.scalar(
        select(PrivatePack.id).where(
            PrivatePack.owner_user_id == user.id,
            PrivatePack.pack_id == pack_id,
            PrivatePack.version == version,
        )
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Pack version already exists")
    item = PrivatePack(
        owner_user_id=user.id,
        pack_id=pack_id,
        version=version,
        title=title,
        kind=kind,
        storage_key="pending",
        sha256="pending",
        size_bytes=0,
        media_type=archive.content_type or "application/octet-stream",
        rights_attested=True,
        distribution="private",
    )
    session.add(item)
    session.flush()
    key = f"users/{user.id}/packs/{item.id}.sagapack"
    storage: LocalPrivateStorage = request.app.state.private_storage
    try:
        checksum, size = storage.put(
            key, archive.file, max_bytes=request.app.state.settings.max_pack_bytes
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    item.storage_key = key
    item.sha256 = checksum
    item.size_bytes = size
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="pack.upload.private",
            subject_type="private_pack",
            subject_id=item.id,
            details={"sha256": checksum, "size_bytes": size, "distribution": "private"},
        )
    )
    session.commit()
    return PrivatePackView.model_validate(item)


@router.get("", response_model=list[PrivatePackView])
def list_private_packs(user: CurrentUser, session: DbSession) -> list[PrivatePackView]:
    return [
        PrivatePackView.model_validate(item)
        for item in session.scalars(
            select(PrivatePack)
            .where(PrivatePack.owner_user_id == user.id)
            .order_by(PrivatePack.created_at.desc())
        ).all()
    ]


@router.post("/{pack_record_id}/campaigns/{campaign_id}/import", response_model=CampaignPackView)
async def import_private_pack(
    pack_record_id: str,
    campaign_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> CampaignPackView:
    _dm_membership(session, campaign_id, user.id)
    pack = session.scalar(
        select(PrivatePack).where(
            PrivatePack.id == pack_record_id,
            PrivatePack.owner_user_id == user.id,
            PrivatePack.status == "private",
        )
    )
    if pack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "private Pack not found")
    existing = session.scalar(
        select(CampaignPackProjection).where(
            CampaignPackProjection.campaign_id == campaign_id,
            CampaignPackProjection.private_pack_id == pack.id,
        )
    )
    if existing is not None:
        return CampaignPackView.model_validate(existing)
    storage: LocalPrivateStorage = request.app.state.private_storage
    exchange_path: Path = storage.materialize_for_runtime(pack.storage_key, pack.id)
    try:
        receipt: dict[str, Any] = await request.app.state.dnd_runtime.import_content_pack(
            campaign_id=campaign_id,
            kind=pack.kind,
            source_path=str(exchange_path),
            principal_id=user.principal_id,
            idempotency_key=f"service-pack:{campaign_id}:{pack.id}:{pack.sha256}",
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    item = CampaignPackProjection(
        campaign_id=campaign_id,
        private_pack_id=pack.id,
        imported_by_user_id=user.id,
        status="imported",
        mcp_receipt=receipt,
    )
    session.add(item)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="pack.import",
            subject_type="campaign_pack",
            subject_id=item.id,
            details={"campaign_id": campaign_id, "private_pack_id": pack.id},
        )
    )
    session.commit()
    return CampaignPackView.model_validate(item)
