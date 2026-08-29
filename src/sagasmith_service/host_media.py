from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sagasmith_service.models import RoomMediaArtifact

HOST_MEDIA_SCHEMA = "sagasmith.host-media/v1"
_MAX_CONTENT_BLOCKS = 64
_SAFE_INLINE_TYPES = frozenset({"text", "image", "audio", "resource", "resource_link"})
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)


@dataclass(frozen=True)
class MaterializedHostMedia:
    row: RoomMediaArtifact
    envelope: dict[str, Any]


def _stable_artifact_id(job_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sagasmith-room-media:{job_id}:{index}"))


def _decode(value: Any, *, max_bytes: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("MCP media block data must be base64 text")
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("MCP media block contains invalid base64") from exc
    if len(payload) > max_bytes:
        raise ValueError("MCP media block exceeds the hosted artifact limit")
    return payload


def _media_type(value: Any) -> str:
    candidate = str(value or "application/octet-stream")[:160]
    return candidate.lower() if _MEDIA_TYPE.fullmatch(candidate) else "application/octet-stream"


def materialize_host_media(
    storage: Any,
    *,
    job_id: str,
    room_id: str,
    campaign_id: str,
    call_tool_results: tuple[dict[str, Any], ...],
    audience: str,
    audience_user_ids: list[str],
    max_bytes: int,
) -> list[MaterializedHostMedia]:
    """Convert standard MCP content blocks to internal object references.

    The input remains a standard ``CallToolResult`` representation.  This
    function is a Host projection step and does not define an alternate MCP
    wire response.
    """

    blocks: list[dict[str, Any]] = []
    for result in call_tool_results:
        content = result.get("content")
        if not isinstance(content, list):
            continue
        blocks.extend(item for item in content if isinstance(item, dict))
    if len(blocks) > _MAX_CONTENT_BLOCKS:
        raise ValueError("Agent returned too many MCP content blocks")

    materialized: list[MaterializedHostMedia] = []
    for index, block in enumerate(blocks):
        kind = str(block.get("type") or "")
        if kind not in _SAFE_INLINE_TYPES or kind == "text":
            continue
        artifact_id = _stable_artifact_id(job_id, index)
        media_type = _media_type(block.get("mimeType"))
        resource_uri: str | None = None
        payload: bytes | None = None
        public_kind = kind
        if kind in {"image", "audio"}:
            payload = _decode(block.get("data"), max_bytes=max_bytes)
        elif kind == "resource":
            resource = block.get("resource")
            if not isinstance(resource, dict):
                raise ValueError("embedded MCP resource is malformed")
            resource_uri = str(resource.get("uri") or "")[:1000] or None
            media_type = _media_type(resource.get("mimeType") or media_type)
            if resource.get("blob") is not None:
                payload = _decode(resource.get("blob"), max_bytes=max_bytes)
            elif resource.get("text") is not None:
                payload = str(resource.get("text")).encode("utf-8")
                if len(payload) > max_bytes:
                    raise ValueError("embedded MCP text resource exceeds the hosted artifact limit")
            else:
                raise ValueError("embedded MCP resource has neither text nor blob")
            public_kind = "embedded-resource"
        else:
            resource_uri = str(block.get("uri") or "")[:1000] or None
            media_type = _media_type(block.get("mimeType") or media_type)
            public_kind = "resource"

        storage_key: str | None = None
        checksum: str | None = None
        size = 0
        if payload is not None:
            checksum = hashlib.sha256(payload).hexdigest()
            storage_key = f"room-media/{campaign_id}/{job_id}/{index}-{checksum[:16]}"
            stored_checksum, size = storage.put_bytes_idempotent(
                storage_key,
                payload,
                max_bytes=max_bytes,
                content_type=media_type,
            )
            if stored_checksum != checksum:
                raise ValueError("hosted MCP media checksum mismatch")

        row = RoomMediaArtifact(
            id=artifact_id,
            job_id=job_id,
            room_id=room_id,
            campaign_id=campaign_id,
            content_index=index,
            kind=public_kind,
            media_type=media_type,
            storage_key=storage_key,
            resource_uri=resource_uri,
            sha256=checksum,
            size_bytes=size,
            audience=audience,
            audience_user_ids=list(audience_user_ids),
        )
        envelope = {
            "schema": HOST_MEDIA_SCHEMA,
            "artifact_id": artifact_id,
            "kind": public_kind,
            "mime_type": media_type,
            "size_bytes": size,
            "sha256": checksum,
            "resource_uri": resource_uri,
            "url": f"/api/campaigns/{campaign_id}/room/artifacts/{artifact_id}",
        }
        materialized.append(MaterializedHostMedia(row, envelope))
    return materialized
