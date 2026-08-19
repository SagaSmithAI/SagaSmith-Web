from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from copy import deepcopy
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.integrations.agent import AgentRuntime
from sagasmith_service.models import (
    ActorBindingProjection,
    AgentConversation,
    AgentIdentity,
    AgentRun,
    ArtifactRelease,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignMessage,
    CampaignProjection,
    CampaignRoom,
    CampaignRoomEvent,
    CampaignRoomReadCursor,
    IdentityCampaignAssignment,
    IdentityMemoryEntry,
    User,
    now_utc,
)
from sagasmith_service.quota import QuotaExceededError, release, reserve, settle
from sagasmith_service.room_activity import RoomActivitySubmission, room_activity_contract
from sagasmith_service.room_turn import (
    PerformanceBlock,
    RoomAudience,
    RoomTurnSubmission,
    plain_text_for_blocks,
    project_speaker,
    room_turn_contract,
)
from sagasmith_service.schemas import (
    CampaignMessageCreate,
    CampaignMessageView,
    CampaignPanelAction,
    CampaignRoomHostUpdate,
    CampaignRoomReadUpdate,
    CampaignRoomSnapshot,
    CampaignRoomView,
)

router = APIRouter(prefix="/api/campaigns/{campaign_id}/room", tags=["campaign-room"])


def _membership(session: Session, campaign_id: str, user_id: str) -> CampaignMembershipProjection:
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


def _room(session: Session, campaign_id: str) -> CampaignRoom:
    item = session.scalar(
        select(CampaignRoom)
        .where(CampaignRoom.campaign_id == campaign_id)
        .with_for_update()
    )
    if item is None:
        campaign = session.get(CampaignProjection, campaign_id)
        if campaign is None or campaign.purpose != "play":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign room not found")
        item = CampaignRoom(campaign_id=campaign_id)
        session.add(item)
        session.flush()
    return item


def _message_visible(
    message: CampaignMessage, membership: CampaignMembershipProjection, user_id: str
) -> bool:
    if message.audience == "public":
        return True
    if message.sender_user_id == user_id:
        return True
    if message.audience == "dm":
        return membership.role in {"owner", "dm"}
    return user_id in set(message.audience_user_ids or [])


def _message_view(
    message: CampaignMessage,
    viewer_user_id: str | None = None,
) -> dict[str, Any]:
    value = CampaignMessageView.model_validate(message).model_dump(mode="json")
    payload = deepcopy(value.get("structured_payload") or {})
    suggestions = payload.get("suggestions")
    if isinstance(suggestions, list):
        payload["suggestions"] = [
            {key: item_value for key, item_value in item.items() if key != "target_user_id"}
            for item in suggestions
            if isinstance(item, dict)
            and item.get("target_user_id") == viewer_user_id
            and not bool(dict(item.get("valid_for") or {}).get("expired", False))
        ]
    value["structured_payload"] = payload
    return value


def _audience_users(
    session: Session,
    campaign_id: str,
    audience: str,
    audience_user_ids: list[str],
) -> set[str]:
    members = session.scalars(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.status == "active",
        )
    ).all()
    if audience == "public":
        return {item.user_id for item in members}
    if audience == "dm":
        return {item.user_id for item in members if item.role in {"owner", "dm"}}
    return {item.user_id for item in members if item.user_id in set(audience_user_ids)}


def _resolve_room_audience(
    session: Session,
    *,
    campaign_id: str,
    requested: RoomAudience,
    trigger: CampaignMessage,
) -> tuple[str, list[str]]:
    if requested.kind == "public":
        audience, user_ids = "public", []
    elif requested.kind == "dm":
        audience, user_ids = "dm", []
    else:
        bindings = session.scalars(
            select(ActorBindingProjection).where(
                ActorBindingProjection.campaign_id == campaign_id,
                ActorBindingProjection.actor_id.in_(requested.actor_refs),
                ActorBindingProjection.status == "active",
                ActorBindingProjection.can_view_private.is_(True),
            )
        ).all()
        bound_refs = {item.actor_id for item in bindings}
        if bound_refs != set(requested.actor_refs):
            raise ValueError("actor audience is not fully bound")
        user_ids = sorted({item.user_id for item in bindings})
        if not user_ids:
            raise ValueError("actor audience has no active recipients")
        audience = "private"
    trigger_users = _audience_users(
        session,
        campaign_id,
        trigger.audience,
        list(trigger.audience_user_ids or []),
    )
    output_users = _audience_users(session, campaign_id, audience, user_ids)
    if not output_users or not output_users.issubset(trigger_users):
        raise ValueError("structured output would broaden the trigger audience")
    return audience, user_ids


def _campaign_runtime(request: Request, session: Session, campaign_id: str) -> Any:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise ValueError("campaign projection not found")
    runtimes = getattr(request.app.state, "game_runtimes", {})
    runtime = runtimes.get(campaign.system_id) if isinstance(runtimes, dict) else None
    if runtime is None and campaign.system_id == "dnd5e":
        runtime = getattr(request.app.state, "dnd_runtime", None)
    if runtime is None:
        raise ValueError(f"no hosted runtime for system {campaign.system_id!r}")
    return runtime


def _activity_token(secret: Any, campaign_id: str, run_id: str) -> str:
    reveal = getattr(secret, "get_secret_value", None)
    secret_value = reveal() if callable(reveal) else str(secret)
    return hmac.new(
        secret_value.encode("utf-8"),
        f"room-activity:{campaign_id}:{run_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _output_resolution_ids(output: Any) -> set[str]:
    identifiers: set[str] = set()
    for block in output.blocks:
        if block.type == "resolution_ref":
            identifiers.add(str(block.resolution_id))
        elif isinstance(block, PerformanceBlock) and block.provenance.kind == "mcp_resolution":
            identifiers.add(str(block.provenance.resolution_id))
    return identifiers


async def _resolution_index(
    request: Request,
    session: Session,
    *,
    submission: RoomTurnSubmission,
    campaign_id: str,
    trigger: CampaignMessage,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Verify every reference by querying the MCP as every target user."""

    runtime = _campaign_runtime(request, session, campaign_id)
    campaign = session.get(CampaignProjection, campaign_id)
    assert campaign is not None
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for output in submission.messages:
        identifiers = _output_resolution_ids(output)
        if not identifiers:
            continue
        audience, audience_user_ids = _resolve_room_audience(
            session,
            campaign_id=campaign_id,
            requested=output.audience,
            trigger=trigger,
        )
        target_user_ids = _audience_users(
            session,
            campaign_id,
            audience,
            audience_user_ids,
        )
        users = [session.get(User, user_id) for user_id in sorted(target_user_ids)]
        if any(user is None for user in users):
            raise ValueError("resolution audience contains an unknown user")
        for resolution_id in identifiers:
            projections: list[dict[str, Any]] = []
            for target in users:
                assert target is not None
                try:
                    projection = await runtime.get_resolution_presentation(
                        campaign_id=campaign_id,
                        resolution_id=resolution_id,
                        principal_id=target.principal_id,
                    )
                except RuntimeError as exc:
                    raise ValueError(
                        "resolution is not visible to the complete message audience"
                    ) from exc
                if (
                    projection.get("schema") != "sagasmith.resolution-presentation/v1"
                    or str(projection.get("resolution_id") or "") != resolution_id
                    or str(projection.get("campaign_id") or "") != campaign_id
                    or str(projection.get("system_id") or "") != campaign.system_id
                ):
                    raise ValueError("MCP returned a mismatched resolution presentation")
                projections.append(dict(projection))
            if not projections:
                raise ValueError("resolution message has no audience")
            comparison = [
                json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                for item in projections
            ]
            if len(set(comparison)) != 1:
                raise ValueError("MCP resolution projection differs inside one message audience")
            indexed[(output.output_id, resolution_id)] = projections[0]
    return indexed


async def _actor_presentation_index(
    request: Request,
    session: Session,
    *,
    submission: RoomTurnSubmission,
    campaign_id: str,
    trigger: CampaignMessage,
) -> dict[tuple[str, str], dict[str, Any]]:
    runtime = _campaign_runtime(request, session, campaign_id)
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for output in submission.messages:
        actor_refs = {
            str(block.speaker.actor_ref)
            for block in output.blocks
            if isinstance(block, PerformanceBlock)
            and block.speaker.kind == "published_actor"
            and block.speaker.actor_ref
        }
        if not actor_refs:
            continue
        audience, audience_user_ids = _resolve_room_audience(
            session,
            campaign_id=campaign_id,
            requested=output.audience,
            trigger=trigger,
        )
        target_user_ids = _audience_users(
            session,
            campaign_id,
            audience,
            audience_user_ids,
        )
        users = [session.get(User, user_id) for user_id in sorted(target_user_ids)]
        for actor_ref in actor_refs:
            presentations: list[dict[str, Any]] = []
            for target in users:
                if target is None:
                    raise ValueError("actor presentation audience contains an unknown user")
                try:
                    actor = await runtime.get_character_card(
                        campaign_id=campaign_id,
                        character_id=actor_ref,
                        principal_id=target.principal_id,
                    )
                except RuntimeError as exc:
                    raise ValueError(
                        "published actor is not visible to the message audience"
                    ) from exc
                if (
                    str(actor.get("id") or "") != actor_ref
                    or str(actor.get("campaign_id") or "") != campaign_id
                    or not str(actor.get("name") or "").strip()
                ):
                    raise ValueError("MCP returned a mismatched actor presentation")
                presentations.append(
                    {
                        "label": str(actor["name"]),
                        "character_type": str(actor.get("character_type") or ""),
                        "revision": int(actor.get("revision") or 0),
                    }
                )
            if not presentations or len(
                {
                    json.dumps(item, sort_keys=True, ensure_ascii=False)
                    for item in presentations
                }
            ) != 1:
                raise ValueError("actor presentation differs inside one message audience")
            indexed[(output.output_id, actor_ref)] = presentations[0]
    suggestion_actor_refs = {
        str(item.actor_ref) for item in submission.suggestions if item.actor_ref
    }
    target = session.get(User, trigger.sender_user_id) if trigger.sender_user_id else None
    if suggestion_actor_refs and target is None:
        raise ValueError("suggestion actor has no triggering principal")
    for actor_ref in suggestion_actor_refs:
        assert target is not None
        try:
            actor = await runtime.get_character_card(
                campaign_id=campaign_id,
                character_id=actor_ref,
                principal_id=target.principal_id,
            )
        except RuntimeError as exc:
            raise ValueError("suggestion actor is not visible to its target") from exc
        if (
            str(actor.get("id") or "") != actor_ref
            or str(actor.get("campaign_id") or "") != campaign_id
        ):
            raise ValueError("MCP returned a mismatched suggestion actor")
        indexed[("__suggestion__", actor_ref)] = {
            "label": str(actor.get("name") or ""),
            "character_type": str(actor.get("character_type") or ""),
            "revision": int(actor.get("revision") or 0),
        }
    return indexed


def _emit(
    session: Session,
    room: CampaignRoom,
    event_type: str,
    payload: dict[str, Any],
) -> CampaignRoomEvent:
    sequence = room.next_event_sequence
    room.next_event_sequence += 1
    event = CampaignRoomEvent(
        room_id=room.id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    return event


def _close_run_activities(
    session: Session,
    room: CampaignRoom,
    *,
    run_id: str,
    state: str,
) -> None:
    latest: dict[str, dict[str, Any]] = {}
    events = session.scalars(
        select(CampaignRoomEvent)
        .where(
            CampaignRoomEvent.room_id == room.id,
            CampaignRoomEvent.event_type == "room.activity",
        )
        .order_by(CampaignRoomEvent.sequence)
    ).all()
    for event in events:
        payload = dict(event.payload or {})
        if payload.get("run_id") == run_id:
            latest[str(payload.get("activity_id") or "")] = payload
    for activity_id, payload in latest.items():
        if not activity_id or payload.get("state") != "started":
            continue
        _emit(
            session,
            room,
            "room.activity",
            {**payload, "state": state},
        )


def _expire_suggestions(
    session: Session,
    room: CampaignRoom,
    *,
    target_user_id: str | None = None,
) -> None:
    messages = session.scalars(
        select(CampaignMessage).where(CampaignMessage.room_id == room.id)
    ).all()
    for message in messages:
        payload = deepcopy(dict(message.structured_payload or {}))
        suggestions = list(payload.get("suggestions") or [])
        changed = False
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            if target_user_id is not None and suggestion.get("target_user_id") != target_user_id:
                continue
            valid_for = dict(suggestion.get("valid_for") or {})
            if valid_for.get("expired"):
                continue
            valid_for["expired"] = True
            suggestion["valid_for"] = valid_for
            changed = True
        if changed:
            payload["suggestions"] = suggestions
            message.structured_payload = payload


def _append_message(
    session: Session,
    room: CampaignRoom,
    *,
    campaign_id: str,
    sender_type: str,
    sender_display_name: str,
    message_type: str,
    audience: str,
    content: str,
    client_message_id: str,
    sender_user_id: str | None = None,
    audience_user_ids: list[str] | None = None,
    structured_payload: dict[str, Any] | None = None,
    reply_to_message_id: str | None = None,
    trigger_message_id: str | None = None,
    status_value: str = "completed",
    mcp_revision: int | None = None,
    mcp_receipt: dict[str, Any] | None = None,
) -> CampaignMessage:
    sequence = room.next_message_sequence
    room.next_message_sequence += 1
    item = CampaignMessage(
        room_id=room.id,
        campaign_id=campaign_id,
        sequence=sequence,
        sender_type=sender_type,
        sender_user_id=sender_user_id,
        sender_display_name=sender_display_name,
        message_type=message_type,
        audience=audience,
        audience_user_ids=list(audience_user_ids or []),
        content=content,
        structured_payload=dict(structured_payload or {}),
        reply_to_message_id=reply_to_message_id,
        trigger_message_id=trigger_message_id,
        mcp_revision=mcp_revision,
        mcp_receipt=dict(mcp_receipt or {}),
        status=status_value,
        client_message_id=client_message_id,
        completed_at=now_utc() if status_value == "completed" else None,
    )
    session.add(item)
    session.flush()
    _emit(session, room, "message.created", {"message_id": item.id})
    return item


def _recent_context(
    session: Session,
    room: CampaignRoom,
    membership: CampaignMembershipProjection,
    user_id: str,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    candidates = session.scalars(
        select(CampaignMessage)
        .where(CampaignMessage.room_id == room.id)
        .order_by(CampaignMessage.sequence.desc())
        .limit(limit * 3)
    ).all()
    visible = [item for item in reversed(candidates) if _message_visible(item, membership, user_id)]
    return [
        {
            "sequence": item.sequence,
            "sender": item.sender_display_name,
            "sender_type": item.sender_type,
            "message_type": item.message_type,
            "content": item.content,
        }
        for item in visible[-limit:]
    ]


def _conversation(
    session: Session,
    campaign_id: str,
    user_id: str,
    identity_assignment_id: str | None = None,
) -> AgentConversation:
    assignment_filter = (
        AgentConversation.identity_assignment_id.is_(None)
        if identity_assignment_id is None
        else AgentConversation.identity_assignment_id == identity_assignment_id
    )
    item = session.scalar(
        select(AgentConversation).where(
            AgentConversation.campaign_id == campaign_id,
            AgentConversation.user_id == user_id,
            assignment_filter,
            AgentConversation.status == "active",
        )
    )
    if item is None:
        item = AgentConversation(
            campaign_id=campaign_id,
            user_id=user_id,
            identity_assignment_id=identity_assignment_id,
            title="战役群聊",
        )
        session.add(item)
        session.flush()
    return item


def _project_turn_messages(
    session: Session,
    *,
    submission: RoomTurnSubmission,
    campaign_id: str,
    run_id: str,
    trigger: CampaignMessage,
    host_role: str,
    user: User,
    secret: Any,
    revision: int | None,
    phase: str | None,
    resolution_index: dict[tuple[str, str], dict[str, Any]],
    actor_presentation_index: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if submission.run_id != run_id:
        raise ValueError("structured output run_id mismatch")

    projected: list[dict[str, Any]] = []
    suggestion_rows: list[dict[str, Any]] = []
    visible_pending_choice_ids = {
        str(pending.get("id") or "")
        for presentation in resolution_index.values()
        for pending in [presentation.get("pending_choice")]
        if isinstance(pending, dict) and str(pending.get("id") or "")
    }
    for suggestion in submission.suggestions:
        actor_revision = suggestion.actor_revision
        if suggestion.actor_ref:
            binding = session.scalar(
                select(ActorBindingProjection.id).where(
                    ActorBindingProjection.campaign_id == campaign_id,
                    ActorBindingProjection.actor_id == suggestion.actor_ref,
                    ActorBindingProjection.user_id == user.id,
                    ActorBindingProjection.status == "active",
                    ActorBindingProjection.can_control.is_(True),
                )
            )
            if binding is None:
                raise ValueError("suggestion actor is outside the triggering principal scope")
            actor_presentation = actor_presentation_index.get(
                ("__suggestion__", suggestion.actor_ref)
            )
            if actor_presentation is None:
                raise ValueError("suggestion actor lacks an MCP presentation")
            authoritative_revision = int(actor_presentation.get("revision") or 0)
            if actor_revision is not None and actor_revision != authoritative_revision:
                raise ValueError("suggestion actor revision is stale")
            actor_revision = authoritative_revision
        if (
            suggestion.pending_choice_id
            and suggestion.pending_choice_id not in visible_pending_choice_ids
        ):
            raise ValueError("suggestion references an unavailable pending choice")
        suggestion_rows.append(
            {
                "id": suggestion.id,
                "text": suggestion.text,
                "actor_ref": suggestion.actor_ref,
                "actor_revision": actor_revision,
                "pending_choice_id": suggestion.pending_choice_id,
                "target_user_id": user.id,
                "valid_for": {
                    "run_id": run_id,
                    "revision": revision,
                    "phase": phase,
                    "actor_ref": suggestion.actor_ref,
                    "actor_revision": actor_revision,
                    "pending_choice_id": suggestion.pending_choice_id,
                    "trigger_sequence": trigger.sequence,
                    "expired": False,
                },
            }
        )

    suggestions_attached = False
    for output in submission.messages:
        audience, audience_user_ids = _resolve_room_audience(
            session,
            campaign_id=campaign_id,
            requested=output.audience,
            trigger=trigger,
        )
        blocks: list[dict[str, Any]] = []
        for block in output.blocks:
            value = block.model_dump(mode="json")
            if isinstance(block, PerformanceBlock):
                actor_ref = block.speaker.actor_ref
                bindings = []
                if actor_ref:
                    bindings = session.scalars(
                        select(ActorBindingProjection).where(
                            ActorBindingProjection.campaign_id == campaign_id,
                            ActorBindingProjection.actor_id == actor_ref,
                            ActorBindingProjection.status == "active",
                        )
                    ).all()
                actor_presentation = (
                    actor_presentation_index.get((output.output_id, actor_ref))
                    if actor_ref
                    else None
                )
                if actor_ref and actor_presentation is None:
                    raise ValueError("published actor lacks an MCP presentation")
                is_human_pc = bool(bindings) and str(
                    (actor_presentation or {}).get("character_type") or ""
                ) in {"pc", "investigator"}
                provenance = block.provenance
                if provenance.kind == "player_intent":
                    owns_actor = any(
                        item.user_id == user.id and item.can_control for item in bindings
                    )
                    if (
                        not actor_ref
                        or not owns_actor
                        or provenance.source_message_id != trigger.id
                    ):
                        raise ValueError("player performance lacks matching owner intent")
                elif provenance.kind == "mcp_resolution":
                    if (output.output_id, str(provenance.resolution_id)) not in resolution_index:
                        raise ValueError("performance references an unverified MCP resolution")
                    if is_human_pc and any(beat.type == "speech" for beat in block.beats):
                        raise ValueError("a mechanic cannot invent speech for a human PC")
                else:
                    if is_human_pc:
                        raise ValueError("Agent rulings cannot choose actions for a human PC")
                    if host_role not in {"owner", "dm"}:
                        raise ValueError("NPC performance requires a DM-scoped host principal")
                value["speaker"] = project_speaker(
                    block.speaker,
                    secret=secret,
                    campaign_id=campaign_id,
                )
                if actor_presentation is not None:
                    value["speaker"]["label"] = actor_presentation["label"]
            elif value.get("type") == "resolution_ref":
                resolution_id = str(value.get("resolution_id") or "")
                presentation = resolution_index.get((output.output_id, resolution_id))
                if presentation is None:
                    raise ValueError("presentation references an unverified MCP resolution")
                value["verified"] = True
                value["authority"] = "mcp"
                value["presentation"] = deepcopy(presentation)
            blocks.append(value)

        output_users = _audience_users(session, campaign_id, audience, audience_user_ids)
        attach_suggestions = not suggestions_attached and user.id in output_users
        payload: dict[str, Any] = {
            "schema": "sagasmith.room-message/v1",
            "run_id": run_id,
            "output_id": output.output_id,
            "blocks": blocks,
            "suggestions": suggestion_rows if attach_suggestions else [],
            "valid_for": {"revision": revision, "phase": phase},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 131_072:
            raise ValueError("structured room message is too large")
        suggestions_attached = suggestions_attached or attach_suggestions
        projected.append(
            {
                "output_id": output.output_id,
                "audience": audience,
                "audience_user_ids": audience_user_ids,
                "blocks": blocks,
                "content": plain_text_for_blocks(blocks),
                "structured_payload": payload,
            }
        )
    if submission.suggestions and not suggestions_attached:
        raise ValueError("suggestions have no message visible to the triggering user")
    return projected


async def _run_agent(
    *,
    request: Request,
    session: Session,
    campaign_id: str,
    room: CampaignRoom,
    membership: CampaignMembershipProjection,
    user: User,
    trigger: CampaignMessage,
    idempotency_key: str,
) -> list[CampaignMessage]:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    principal_id = user.principal_id
    host_role = membership.role
    quota_user_id = user.id
    conversation_user_id = user.id
    identity_assignment_id: str | None = None
    identity_context: dict[str, Any] = {}
    sender_display_name = "SagaSmith"
    session_id: str | None = None
    if room.host_identity_assignment_id:
        assignment = session.scalar(
            select(IdentityCampaignAssignment).where(
                IdentityCampaignAssignment.id == room.host_identity_assignment_id,
                IdentityCampaignAssignment.campaign_id == campaign_id,
                IdentityCampaignAssignment.status == "accepted",
                IdentityCampaignAssignment.role == "dm",
            )
        )
        identity = session.get(AgentIdentity, assignment.identity_id) if assignment else None
        soul = session.get(ArtifactRelease, assignment.soul_release_id) if assignment else None
        if (
            assignment is None
            or identity is None
            or soul is None
            or identity.status != "active"
            or soul.status != "published"
        ):
            trigger.status = "failed"
            trigger.completed_at = now_utc()
            _emit(
                session,
                room,
                "agent.failed",
                {"message_id": trigger.id, "error_code": "host_identity_unavailable"},
            )
            session.commit()
            raise HTTPException(status.HTTP_409_CONFLICT, "host Identity is unavailable")
        memories = session.scalars(
            select(IdentityMemoryEntry)
            .where(IdentityMemoryEntry.assignment_id == assignment.id)
            .order_by(IdentityMemoryEntry.memory_key)
            .limit(100)
        ).all()
        principal_id = identity.principal_id
        host_role = assignment.role
        quota_user_id = assignment.quota_payer_user_id
        conversation_user_id = assignment.invited_by_user_id
        identity_assignment_id = assignment.id
        sender_display_name = identity.name
        identity_context = {
            "identity": {
                "id": identity.id,
                "name": identity.name,
                "kind": identity.identity_kind,
                "memory_namespace": assignment.memory_namespace,
            },
            "soul": soul.payload,
            "campaign_memory": [
                {
                    "key": item.memory_key,
                    "content": item.content,
                    "audience": item.audience,
                    "revision": item.revision,
                }
                for item in memories
            ],
        }
    conversation = _conversation(
        session,
        campaign_id,
        conversation_user_id,
        identity_assignment_id,
    )
    context_membership = (
        _membership(session, campaign_id, conversation_user_id)
        if identity_assignment_id is not None
        else membership
    )
    context_user_id = conversation_user_id if identity_assignment_id is not None else user.id
    session_id = (
        f"{campaign_id}:agent:{identity.id}:{conversation.id}"
        if identity_assignment_id is not None
        else f"{campaign_id}:{user.id}:{conversation.id}"
    )
    reservation_quantity = Decimal(request.app.state.settings.agent_reservation_tokens)
    try:
        reservation = reserve(
            session,
            user_id=quota_user_id,
            campaign_id=campaign_id,
            metric="llm_tokens",
            quantity=reservation_quantity,
            idempotency_key=f"room-agent-reserve:{user.id}:{idempotency_key}",
            ttl_seconds=300,
        )
    except QuotaExceededError as exc:
        trigger.status = "failed"
        trigger.completed_at = now_utc()
        _emit(
            session,
            room,
            "agent.failed",
            {"message_id": trigger.id, "error_code": "quota_exceeded"},
        )
        session.commit()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc

    run = AgentRun(
        conversation_id=conversation.id,
        trigger_message_id=trigger.id,
        campaign_id=campaign_id,
        user_id=user.id,
        idempotency_key=f"room:{idempotency_key}",
        request_hash=hashlib.sha256(trigger.content.encode()).hexdigest(),
        user_content=trigger.content,
    )
    session.add(run)
    session.flush()
    _emit(session, room, "agent.started", {"message_id": trigger.id, "run_id": run.id})
    session.commit()

    runtime: AgentRuntime = request.app.state.agent_runtime
    settings = request.app.state.settings
    activity_callback = (
        f"{settings.service_internal_url.rstrip('/')}/api/campaigns/{campaign_id}"
        f"/room/internal-activity/{run.id}"
    )
    try:
        result = await runtime.complete(
            session_id=session_id,
            content=trigger.content,
            context={
                "campaign_id": campaign_id,
                "system_id": campaign.system_id,
                "principal_id": principal_id,
                "campaign_role": host_role,
                "room_id": room.id,
                "room_context": _recent_context(
                    session,
                    room,
                    context_membership,
                    context_user_id,
                ),
                "action_context": dict(trigger.structured_payload or {}),
                "run_id": run.id,
                "trigger_message_id": trigger.id,
                "response_contract": {
                    "terminal": room_turn_contract(),
                    "activity": room_activity_contract(),
                    "activity_callback": {
                        "url": activity_callback,
                        "token": _activity_token(settings.session_secret, campaign_id, run.id),
                    },
                },
                **identity_context,
            },
        )
    except RuntimeError as exc:
        # The Agent may have emitted room activity through the callback while
        # this request was awaiting completion. Reacquire the room row and its
        # authoritative counter before allocating another event sequence.
        session.refresh(room, attribute_names=["next_event_sequence"], with_for_update=True)
        release(session, reservation.id)
        trigger.status = "failed"
        trigger.completed_at = now_utc()
        run.status = "failed"
        run.error_code = "agent_unavailable"
        run.completed_at = now_utc()
        _close_run_activities(session, room, run_id=run.id, state="failed")
        _emit(
            session,
            room,
            "agent.failed",
            {"message_id": trigger.id, "run_id": run.id, "error_code": run.error_code},
        )
        session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    # Activity callbacks use independent transactions, so the identity-mapped
    # room object can be stale even though every callback held the row lock.
    session.refresh(room, attribute_names=["next_event_sequence"], with_for_update=True)

    actual = min(result.total_tokens, int(reservation_quantity))
    settle(
        session,
        reservation_id=reservation.id,
        quantity=Decimal(actual),
        idempotency_key=f"room-agent-settle:{user.id}:{idempotency_key}",
        unit="tokens",
        provider="nanobot",
        model=result.model,
        request_id=result.request_id,
    )
    run.upstream_request_id = result.request_id
    run.model = result.model
    run.prompt_tokens = result.prompt_tokens
    run.completion_tokens = result.completion_tokens

    revision: int | None = None
    phase: str | None = None
    try:
        runtime_state = await _campaign_runtime(request, session, campaign_id).get_campaign(
            campaign_id=campaign_id, principal_id=principal_id
        )
        state = runtime_state.get("result", runtime_state)
        revision = int(state.get("revision") or state.get("campaign_revision") or 0) or None
        phase_value = (
            state.get("effective_game_phase")
            or state.get("phase")
            or state.get("game_phase")
        )
        phase = str(phase_value) if phase_value else None
        campaign = session.get(CampaignProjection, campaign_id)
        if campaign is not None and revision is not None:
            campaign.mcp_revision = revision
    except RuntimeError:
        pass
    try:
        if result.structured_output is None:
            raise ValueError("Agent returned no structured room output")
        submission = RoomTurnSubmission.model_validate(result.structured_output)
        resolution_index = await _resolution_index(
            request,
            session,
            submission=submission,
            campaign_id=campaign_id,
            trigger=trigger,
        )
        actor_presentation_index = await _actor_presentation_index(
            request,
            session,
            submission=submission,
            campaign_id=campaign_id,
            trigger=trigger,
        )
        projected = _project_turn_messages(
            session,
            submission=submission,
            campaign_id=campaign_id,
            run_id=run.id,
            trigger=trigger,
            host_role=host_role,
            user=user,
            secret=request.app.state.settings.session_secret,
            revision=revision,
            phase=phase,
            resolution_index=resolution_index,
            actor_presentation_index=actor_presentation_index,
        )
    except (ValidationError, ValueError) as exc:
        trigger.status = "failed"
        trigger.completed_at = now_utc()
        run.status = "failed"
        run.error_code = "agent_invalid_output"
        run.assistant_content = result.content
        run.completed_at = now_utc()
        _close_run_activities(session, room, run_id=run.id, state="failed")
        _emit(
            session,
            room,
            "agent.failed",
            {"message_id": trigger.id, "run_id": run.id, "error_code": run.error_code},
        )
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="campaign.room.agent.invalid_output",
                subject_type="campaign_message",
                subject_id=trigger.id,
                details={"campaign_id": campaign_id, "run_id": run.id},
            )
        )
        session.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Agent returned an invalid structured room response",
        ) from exc

    assistants: list[CampaignMessage] = []
    for output in projected:
        assistant = _append_message(
            session,
            room,
            campaign_id=campaign_id,
            sender_type="agent",
            sender_display_name=sender_display_name,
            message_type="presentation",
            audience=output["audience"],
            audience_user_ids=output["audience_user_ids"],
            content=output["content"],
            client_message_id=f"agent:{run.id}:{output['output_id']}",
            trigger_message_id=trigger.id,
            structured_payload=output["structured_payload"],
            mcp_revision=revision,
        )
        assistants.append(assistant)

    run.assistant_content = "\n\n".join(item.content for item in assistants)
    run.status = "completed"
    run.completed_at = now_utc()
    trigger.status = "completed"
    trigger.completed_at = now_utc()
    _close_run_activities(session, room, run_id=run.id, state="superseded")
    _emit(
        session,
        room,
        "agent.completed",
        {
            "message_id": trigger.id,
            "agent_message_ids": [item.id for item in assistants],
            "run_id": run.id,
        },
    )
    _emit(
        session,
        room,
        "state.changed",
        {"reason": "agent.completed", "mcp_revision": revision},
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.room.agent.complete",
            subject_type="campaign_message",
            subject_id=trigger.id,
            details={
                "campaign_id": campaign_id,
                "tokens": actual,
                "run_id": run.id,
                "auth_context_receipts": [
                    dict(receipt["auth_context_receipt"])
                    for receipt in result.tool_receipts
                    if isinstance(receipt.get("auth_context_receipt"), dict)
                ],
            },
        )
    )
    session.commit()
    return assistants


async def _post_message(
    *,
    campaign_id: str,
    payload: CampaignMessageCreate,
    request: Request,
    user: User,
    session: Session,
    idempotency_key: str,
) -> dict[str, Any]:
    membership = _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
    existing = session.scalar(
        select(CampaignMessage).where(
            CampaignMessage.room_id == room.id,
            CampaignMessage.client_message_id == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.sender_user_id != user.id
            or existing.content != payload.content
            or existing.message_type != payload.mode
            or existing.audience != payload.audience
            or list(existing.audience_user_ids or []) != payload.audience_user_ids
            or existing.reply_to_message_id != payload.reply_to_message_id
            or dict(existing.structured_payload or {}) != payload.structured_payload
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key payload mismatch")
        response: dict[str, Any] = {
            "message": _message_view(existing, user.id),
            "agent_message": None,
        }
        assistants = session.scalars(
            select(CampaignMessage)
            .where(CampaignMessage.trigger_message_id == existing.id)
            .order_by(CampaignMessage.sequence)
        ).all()
        visible_assistant = next(
            (
                item
                for item in assistants
                if _message_visible(item, membership, user.id)
            ),
            None,
        )
        if visible_assistant is not None:
            response["agent_message"] = _message_view(visible_assistant, user.id)
        return response
    if payload.mode == "narration" and membership.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required for narration")
    if payload.audience == "private":
        active_users = set(
            session.scalars(
                select(CampaignMembershipProjection.user_id).where(
                    CampaignMembershipProjection.campaign_id == campaign_id,
                    CampaignMembershipProjection.status == "active",
                    CampaignMembershipProjection.user_id.in_(payload.audience_user_ids),
                )
            ).all()
        )
        if active_users != set(payload.audience_user_ids):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "private audience is invalid"
            )
    if payload.reply_to_message_id:
        parent = session.get(CampaignMessage, payload.reply_to_message_id)
        if parent is None or parent.room_id != room.id or not _message_visible(
            parent, membership, user.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "reply target not found")

    if payload.mode == "action":
        _expire_suggestions(session, room, target_user_id=user.id)

    message = _append_message(
        session,
        room,
        campaign_id=campaign_id,
        sender_type="user",
        sender_user_id=user.id,
        sender_display_name=user.display_name,
        message_type=payload.mode,
        audience=payload.audience,
        audience_user_ids=payload.audience_user_ids,
        content=payload.content,
        client_message_id=idempotency_key,
        structured_payload=payload.structured_payload,
        reply_to_message_id=payload.reply_to_message_id,
        status_value="processing" if payload.mode == "action" else "completed",
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.room.message.create",
            subject_type="campaign_message",
            subject_id=message.id,
            details={"campaign_id": campaign_id, "mode": payload.mode},
        )
    )
    session.commit()
    assistants: list[CampaignMessage] = []
    if payload.mode == "action":
        assistants = await _run_agent(
            request=request,
            session=session,
            campaign_id=campaign_id,
            room=room,
            membership=membership,
            user=user,
            trigger=message,
            idempotency_key=idempotency_key,
        )
    visible_assistant = next(
        (item for item in assistants if _message_visible(item, membership, user.id)),
        None,
    )
    return {
        "message": _message_view(message, user.id),
        "agent_message": (
            _message_view(visible_assistant, user.id)
            if visible_assistant is not None
            else None
        ),
    }


@router.post("/internal-activity/{run_id}")
def report_room_activity(
    campaign_id: str,
    run_id: str,
    payload: RoomActivitySubmission,
    request: Request,
    session: DbSession,
    authorization: Annotated[str, Header(alias="Authorization")],
) -> dict[str, bool]:
    expected = "Bearer " + _activity_token(
        request.app.state.settings.session_secret,
        campaign_id,
        run_id,
    )
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid activity token")
    if payload.run_id != run_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "activity run mismatch")
    run = session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.campaign_id == campaign_id)
    )
    if run is None or run.status not in {"pending", "running"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent run is no longer active")
    trigger = session.get(CampaignMessage, run.trigger_message_id)
    if trigger is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent trigger is unavailable")
    room = _room(session, campaign_id)
    if payload.code in {"resolving_roll", "settling_save"} and payload.audience.kind == "public":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "mechanical activity cannot use public audience",
        )
    try:
        audience, audience_user_ids = _resolve_room_audience(
            session,
            campaign_id=campaign_id,
            requested=payload.audience,
            trigger=trigger,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    previous = session.scalars(
        select(CampaignRoomEvent).where(
            CampaignRoomEvent.room_id == room.id,
            CampaignRoomEvent.event_type == "room.activity",
        )
    ).all()
    states = [
        str(item.payload.get("state") or "")
        for item in previous
        if item.payload.get("run_id") == run_id
        and item.payload.get("activity_id") == payload.activity_id
    ]
    if states and states[-1] == payload.state:
        return {"accepted": True}
    terminal = {"completed", "failed", "cancelled", "superseded"}
    if (not states and payload.state != "started") or (states and states[-1] in terminal):
        raise HTTPException(status.HTTP_409_CONFLICT, "invalid activity state transition")
    _emit(
        session,
        room,
        "room.activity",
        {
            "schema": payload.schema_version,
            "run_id": run_id,
            "activity_id": payload.activity_id,
            "audience": audience,
            "audience_user_ids": audience_user_ids,
            "code": payload.code,
            "state": payload.state,
        },
    )
    session.commit()
    return {"accepted": True}


@router.get("", response_model=CampaignRoomView)
def get_room(campaign_id: str, user: CurrentUser, session: DbSession) -> CampaignRoomView:
    _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
    session.commit()
    return CampaignRoomView.model_validate(room)


@router.put("/host", response_model=CampaignRoomView)
def set_room_host(
    campaign_id: str,
    payload: CampaignRoomHostUpdate,
    user: CurrentUser,
    session: DbSession,
) -> CampaignRoomView:
    membership = _membership(session, campaign_id, user.id)
    if membership.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign owner required")
    room = _room(session, campaign_id)
    if payload.identity_assignment_id is not None:
        assignment = session.scalar(
            select(IdentityCampaignAssignment).where(
                IdentityCampaignAssignment.id == payload.identity_assignment_id,
                IdentityCampaignAssignment.campaign_id == campaign_id,
                IdentityCampaignAssignment.status == "accepted",
                IdentityCampaignAssignment.role == "dm",
            )
        )
        identity = session.get(AgentIdentity, assignment.identity_id) if assignment else None
        if assignment is None or identity is None or identity.status != "active":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "accepted active DM Identity assignment required",
            )
    room.host_identity_assignment_id = payload.identity_assignment_id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action="campaign.room.host.set",
            subject_type="campaign_room",
            subject_id=room.id,
            details={"identity_assignment_id": payload.identity_assignment_id},
        )
    )
    _emit(
        session,
        room,
        "host.changed",
        {"identity_assignment_id": payload.identity_assignment_id},
    )
    session.commit()
    return CampaignRoomView.model_validate(room)


@router.get("/snapshot", response_model=CampaignRoomSnapshot)
def room_snapshot(
    campaign_id: str,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> CampaignRoomSnapshot:
    membership = _membership(session, campaign_id, user.id)
    # The room row serializes message/event sequence allocation. Holding this
    # lock until the snapshot cursor is captured prevents a load/subscribe gap.
    room = _room(session, campaign_id)
    candidates = session.scalars(
        select(CampaignMessage)
        .where(CampaignMessage.room_id == room.id)
        .order_by(CampaignMessage.sequence.desc())
        .limit(limit * 3)
    ).all()
    visible = [
        CampaignMessageView.model_validate(_message_view(item, user.id))
        for item in reversed(candidates)
        if _message_visible(item, membership, user.id)
    ][-limit:]
    result = CampaignRoomSnapshot(
        room=CampaignRoomView.model_validate(room),
        messages=visible,
        event_cursor=max(0, room.next_event_sequence - 1),
    )
    session.commit()
    return result


@router.get("/messages", response_model=list[CampaignMessageView])
def list_messages(
    campaign_id: str,
    user: CurrentUser,
    session: DbSession,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[CampaignMessageView]:
    membership = _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
    candidates = session.scalars(
        select(CampaignMessage)
        .where(CampaignMessage.room_id == room.id, CampaignMessage.sequence > after)
        .order_by(CampaignMessage.sequence)
        .limit(limit * 3)
    ).all()
    return [
        CampaignMessageView.model_validate(_message_view(item, user.id))
        for item in candidates
        if _message_visible(item, membership, user.id)
    ][:limit]


@router.post("/messages")
async def post_message(
    campaign_id: str,
    payload: CampaignMessageCreate,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> dict[str, Any]:
    return await _post_message(
        campaign_id=campaign_id,
        payload=payload,
        request=request,
        user=user,
        session=session,
        idempotency_key=idempotency_key,
    )


@router.put("/read")
def update_read_cursor(
    campaign_id: str,
    payload: CampaignRoomReadUpdate,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, int]:
    _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
    maximum = max(0, room.next_message_sequence - 1)
    if payload.last_read_sequence > maximum:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "read cursor exceeds room")
    item = session.scalar(
        select(CampaignRoomReadCursor).where(
            CampaignRoomReadCursor.room_id == room.id,
            CampaignRoomReadCursor.user_id == user.id,
        )
    )
    if item is None:
        item = CampaignRoomReadCursor(room_id=room.id, user_id=user.id)
        session.add(item)
    item.last_read_sequence = max(item.last_read_sequence or 0, payload.last_read_sequence)
    session.commit()
    return {"last_read_sequence": item.last_read_sequence}


@router.get("/events")
async def room_events(
    campaign_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> StreamingResponse:
    _membership(session, campaign_id, user.id)
    room_id = _room(session, campaign_id).id
    session.commit()
    session_factory = request.app.state.session_factory

    async def stream():
        cursor = max(after, last_event_id or 0)
        idle_ticks = 0
        while not await request.is_disconnected():
            with session_factory() as poll:
                membership = poll.scalar(
                    select(CampaignMembershipProjection).where(
                        CampaignMembershipProjection.campaign_id == campaign_id,
                        CampaignMembershipProjection.user_id == user.id,
                        CampaignMembershipProjection.status == "active",
                    )
                )
                if membership is None:
                    yield "event: access.revoked\ndata: {}\n\n"
                    return
                events = poll.scalars(
                    select(CampaignRoomEvent)
                    .where(
                        CampaignRoomEvent.room_id == room_id,
                        CampaignRoomEvent.sequence > cursor,
                    )
                    .order_by(CampaignRoomEvent.sequence)
                    .limit(100)
                ).all()
                for event in events:
                    cursor = event.sequence
                    payload = dict(event.payload or {})
                    if event.event_type == "room.activity":
                        event_audience = str(payload.get("audience") or "public")
                        visible = event_audience == "public"
                        if event_audience == "dm":
                            visible = membership.role in {"owner", "dm"}
                        elif event_audience == "private":
                            visible = user.id in set(payload.get("audience_user_ids") or [])
                        if not visible:
                            continue
                    message_id = payload.get("message_id")
                    if message_id:
                        message = poll.get(CampaignMessage, str(message_id))
                        if message is None or not _message_visible(message, membership, user.id):
                            continue
                        payload["message"] = _message_view(message, user.id)
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 30:
                    yield ": keepalive\n\n"
                    idle_ticks = 0
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/panel")
async def panel_state(
    campaign_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    membership = _membership(session, campaign_id, user.id)
    runtime = _campaign_runtime(request, session, campaign_id)
    try:
        value = await runtime.get_panel_state(
            campaign_id=campaign_id, principal_id=user.principal_id
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    bindings = session.scalars(
        select(ActorBindingProjection).where(
            ActorBindingProjection.campaign_id == campaign_id,
            ActorBindingProjection.status == "active",
        )
    ).all()
    visible_bindings = bindings if membership.role in {"owner", "dm"} else [
        item for item in bindings if item.user_id == user.id
    ]
    return {
        **value,
        "membership": {"role": membership.role, "user_id": user.id},
        "actor_bindings": [
            {
                "actor_id": item.actor_id,
                "user_id": item.user_id,
                "can_control": item.can_control,
                "can_view_private": item.can_view_private,
            }
            for item in visible_bindings
        ],
    }


@router.get("/characters/{actor_id}")
async def character_card(
    campaign_id: str,
    actor_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    membership = _membership(session, campaign_id, user.id)
    binding = session.scalar(
        select(ActorBindingProjection).where(
            ActorBindingProjection.campaign_id == campaign_id,
            ActorBindingProjection.actor_id == actor_id,
            ActorBindingProjection.user_id == user.id,
            ActorBindingProjection.status == "active",
        )
    )
    is_dm = membership.role in {"owner", "dm"}
    if not is_dm and (binding is None or not binding.can_view_private):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "private actor view required")
    runtime = _campaign_runtime(request, session, campaign_id)
    try:
        actor = await runtime.get_character_card(
            campaign_id=campaign_id,
            character_id=actor_id,
            principal_id=user.principal_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "character card unavailable") from exc
    if str(actor.get("campaign_id") or "") != campaign_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")
    return {
        "actor": actor,
        "permissions": {
            "can_control": is_dm or bool(binding and binding.can_control),
            "can_view_private": is_dm or bool(binding and binding.can_view_private),
        },
    }


def _receipt_revision(receipt: dict[str, Any], fallback: int) -> int:
    result = receipt.get("result", receipt)
    return int(result.get("campaign_revision") or result.get("revision") or fallback)


@router.post("/panel/actions")
async def panel_action(
    campaign_id: str,
    payload: CampaignPanelAction,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> dict[str, Any]:
    membership = _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
    if payload.action.endswith(".intent"):
        intent = str(payload.payload.get("intent") or "").strip()
        if not intent:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "intent is required")
        actor_id = str(payload.payload.get("actor_id") or "").strip()
        prefix = f"角色 {actor_id}：" if actor_id else ""
        return await _post_message(
            campaign_id=campaign_id,
            payload=CampaignMessageCreate(
                content=f"{prefix}{intent}",
                mode="action",
                audience="public",
                structured_payload={"panel_action": payload.action, **payload.payload},
            ),
            request=request,
            user=user,
            session=session,
            idempotency_key=idempotency_key,
        )
    existing = session.scalar(
        select(CampaignMessage).where(
            CampaignMessage.room_id == room.id,
            CampaignMessage.client_message_id == f"panel:{idempotency_key}",
        )
    )
    if existing is not None:
        recorded = dict(existing.structured_payload or {})
        if (
            recorded.get("panel_action") != payload.action
            or recorded.get("payload") != payload.payload
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key payload mismatch")
        return {"message": _message_view(existing, user.id), "receipt": existing.mcp_receipt}
    if membership.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    runtime = _campaign_runtime(request, session, campaign_id)
    panel = await runtime.get_panel_state(campaign_id=campaign_id, principal_id=user.principal_id)
    revision = int(panel.get("revision") or 0)
    try:
        if payload.action == "phase.set":
            target = str(payload.payload.get("phase") or "")
            if target not in {"lobby", "play"}:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid phase")
            receipt = await runtime.set_game_phase(
                campaign_id=campaign_id,
                principal_id=user.principal_id,
                tool_profile=target,
                expected_revision=revision,
                idempotency_key=f"service-panel:{campaign_id}:{idempotency_key}",
            )
            content = f"战役阶段已切换为 {target.upper()}。"
            message_type = "status"
        elif payload.action == "combat.start":
            participant_ids = [str(item) for item in payload.payload.get("participant_ids") or []]
            if not participant_ids:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "combat participants are required"
                )
            mode = str(payload.payload.get("positioning_mode") or "agent")
            if mode not in {"agent", "grid"}:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid combat mode")
            raw_config = payload.payload.get("participant_config") or []
            if not isinstance(raw_config, list) or any(
                not isinstance(item, dict) for item in raw_config
            ):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "participant_config must be an array of objects",
                )
            raw_map = payload.payload.get("battle_map")
            if raw_map is not None and not isinstance(raw_map, dict):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "battle_map must be an object"
                )
            if mode == "grid" and (not raw_map or len(raw_config) != len(participant_ids)):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "grid combat requires a battle map and one participant config per actor",
                )
            receipt = await runtime.start_combat(
                campaign_id=campaign_id,
                principal_id=user.principal_id,
                participant_ids=participant_ids,
                positioning_mode=mode,
                name=str(payload.payload.get("name") or "Combat")[:160],
                participant_config=list(raw_config),
                battle_map=(dict(raw_map) if mode == "grid" else None),
                battle_map_override_reason=(
                    str(payload.payload.get("battle_map_override_reason") or "")[:2000]
                    if mode == "grid"
                    else None
                ),
                expected_revision=revision,
                idempotency_key=f"service-panel:{campaign_id}:{idempotency_key}",
            )
            content = f"战斗开始：{payload.payload.get('name') or 'Combat'}。"
            message_type = "combat"
        else:
            outcome = {
                "status": str(payload.payload.get("status") or "completed"),
                "summary": str(payload.payload.get("summary") or "战斗结束。")[:2000],
            }
            receipt = await runtime.end_combat(
                campaign_id=campaign_id,
                principal_id=user.principal_id,
                outcome=outcome,
                expected_revision=revision,
                idempotency_key=f"service-panel:{campaign_id}:{idempotency_key}",
            )
            content = outcome["summary"]
            message_type = "combat"
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    next_revision = _receipt_revision(receipt, revision)
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is not None:
        campaign.mcp_revision = next_revision
    _expire_suggestions(session, room)
    message = _append_message(
        session,
        room,
        campaign_id=campaign_id,
        sender_type="system",
        sender_display_name="SagaSmith System",
        message_type=message_type,
        audience="public",
        content=content,
        client_message_id=f"panel:{idempotency_key}",
        structured_payload={"panel_action": payload.action, "payload": payload.payload},
        mcp_revision=next_revision,
        mcp_receipt=receipt,
    )
    _emit(
        session,
        room,
        "state.changed",
        {"reason": payload.action, "mcp_revision": next_revision},
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            action=f"campaign.panel.{payload.action}",
            subject_type="campaign_message",
            subject_id=message.id,
            details={"campaign_id": campaign_id, "mcp_revision": next_revision},
        )
    )
    session.commit()
    return {"message": _message_view(message, user.id), "receipt": receipt}
