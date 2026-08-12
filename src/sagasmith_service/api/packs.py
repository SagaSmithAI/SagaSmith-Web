from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.models import (
    AuditEvent,
    CampaignMembershipProjection,
    CampaignPackProjection,
    PrivatePack,
)
from sagasmith_service.pack_archive import (
    ARCHIVE_EXTENSION,
    PackArchiveError,
    validate_pack_archive,
)
from sagasmith_service.schemas import CampaignPackView, PrivatePackView
from sagasmith_service.storage import LocalPrivateStorage, PrivateStorageError

router = APIRouter(prefix="/api/packs", tags=["private-packs"])


def _receipt_result(receipt: dict[str, Any]) -> dict[str, Any]:
    value = receipt.get("result", receipt)
    return value if isinstance(value, dict) else {}


def _runtime_ref(pack: PrivatePack, receipt: dict[str, Any]) -> str:
    result = _receipt_result(receipt)
    if pack.kind == "module":
        value = result.get("module_id")
        if not value:
            raise RuntimeError("D&D MCP Pack receipt has no module id")
        return str(value)
    if pack.kind == "addon":
        addon = result.get("addon")
        if isinstance(addon, dict) and addon.get("addon_id"):
            return str(addon["addon_id"])
    return pack.pack_id


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
    if not (archive.filename or "").casefold().endswith(ARCHIVE_EXTENSION):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"archive must use the current {ARCHIVE_EXTENSION} format",
        )
    try:
        validate_pack_archive(
            archive.file,
            expected_id=pack_id,
            expected_version=version,
            expected_kind=kind,
            max_bytes=request.app.state.settings.max_pack_bytes,
            max_uncompressed_bytes=request.app.state.settings.max_pack_uncompressed_bytes,
        )
    except PackArchiveError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
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
    key = f"users/{user.id}/packs/{item.id}{ARCHIVE_EXTENSION}"
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


@router.get("/campaigns/{campaign_id}", response_model=list[CampaignPackView])
def list_campaign_packs(
    campaign_id: str, user: CurrentUser, session: DbSession
) -> list[CampaignPackView]:
    member = session.scalar(
        select(CampaignMembershipProjection.id).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user.id,
            CampaignMembershipProjection.status == "active",
        )
    )
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign membership required")
    items = session.scalars(
        select(CampaignPackProjection)
        .where(CampaignPackProjection.campaign_id == campaign_id)
        .order_by(CampaignPackProjection.created_at.desc())
    ).all()
    return [CampaignPackView.model_validate(item) for item in items]


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
    try:
        exchange_path: Path = storage.materialize_for_runtime(pack.storage_key, pack.id)
    except PrivateStorageError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "private Pack storage is temporarily unavailable",
        ) from exc
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
    finally:
        exchange_path.unlink(missing_ok=True)
    try:
        runtime_ref = _runtime_ref(pack, receipt)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    item = CampaignPackProjection(
        campaign_id=campaign_id,
        private_pack_id=pack.id,
        imported_by_user_id=user.id,
        status="imported",
        runtime_ref=runtime_ref,
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


@router.post(
    "/{pack_record_id}/campaigns/{campaign_id}/activate",
    response_model=CampaignPackView,
)
async def activate_private_pack(
    pack_record_id: str,
    campaign_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> CampaignPackView:
    _dm_membership(session, campaign_id, user.id)
    item = session.scalar(
        select(CampaignPackProjection)
        .where(
            CampaignPackProjection.campaign_id == campaign_id,
            CampaignPackProjection.private_pack_id == pack_record_id,
        )
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Pack must be imported before activation")
    if item.status == "activated":
        return CampaignPackView.model_validate(item)
    pack = session.get(PrivatePack, pack_record_id)
    if pack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "private Pack not found")
    if pack.kind == "preset":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "preset Packs are imported into the actor library and are not campaign-activated",
        )
    runtime_ref = item.runtime_ref or _runtime_ref(pack, item.mcp_receipt)
    try:
        receipt = await request.app.state.dnd_runtime.activate_content_pack(
            campaign_id=campaign_id,
            kind=pack.kind,
            runtime_ref=runtime_ref,
            pack_id=pack.pack_id,
            version=pack.version,
            principal_id=user.principal_id,
            idempotency_key=f"service-pack-activate:{campaign_id}:{pack.id}:{idempotency_key}",
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    item.status = "activated"
    item.runtime_ref = runtime_ref
    item.mcp_receipt = {"import": item.mcp_receipt, "activation": receipt}
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="pack.activate",
            subject_type="campaign_pack",
            subject_id=item.id,
            details={
                "campaign_id": campaign_id,
                "private_pack_id": pack.id,
                "runtime_ref": runtime_ref,
            },
        )
    )
    session.commit()
    return CampaignPackView.model_validate(item)
