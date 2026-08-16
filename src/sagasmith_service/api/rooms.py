from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.integrations.agent import AgentRuntime
from sagasmith_service.integrations.dnd_mcp import DndRuntime
from sagasmith_service.models import (
    ActorBindingProjection,
    AgentConversation,
    AgentRun,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignMessage,
    CampaignProjection,
    CampaignRoom,
    CampaignRoomEvent,
    CampaignRoomReadCursor,
    User,
    now_utc,
)
from sagasmith_service.quota import QuotaExceededError, release, reserve, settle
from sagasmith_service.schemas import (
    CampaignMessageCreate,
    CampaignMessageView,
    CampaignPanelAction,
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


def _message_view(message: CampaignMessage) -> dict[str, Any]:
    return CampaignMessageView.model_validate(message).model_dump(mode="json")


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


def _conversation(session: Session, campaign_id: str, user_id: str) -> AgentConversation:
    item = session.scalar(
        select(AgentConversation).where(
            AgentConversation.campaign_id == campaign_id,
            AgentConversation.user_id == user_id,
            AgentConversation.identity_assignment_id.is_(None),
            AgentConversation.status == "active",
        )
    )
    if item is None:
        item = AgentConversation(
            campaign_id=campaign_id,
            user_id=user_id,
            title="战役群聊",
        )
        session.add(item)
        session.flush()
    return item


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
) -> CampaignMessage:
    conversation = _conversation(session, campaign_id, user.id)
    reservation_quantity = Decimal(request.app.state.settings.agent_reservation_tokens)
    try:
        reservation = reserve(
            session,
            user_id=user.id,
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
    try:
        result = await runtime.complete(
            session_id=f"{campaign_id}:{user.id}:{conversation.id}",
            content=trigger.content,
            context={
                "campaign_id": campaign_id,
                "principal_id": user.principal_id,
                "campaign_role": membership.role,
                "room_id": room.id,
                "room_context": _recent_context(session, room, membership, user.id),
                "action_context": dict(trigger.structured_payload or {}),
            },
        )
    except RuntimeError as exc:
        release(session, reservation.id)
        trigger.status = "failed"
        trigger.completed_at = now_utc()
        run.status = "failed"
        run.error_code = "agent_unavailable"
        run.completed_at = now_utc()
        _emit(
            session,
            room,
            "agent.failed",
            {"message_id": trigger.id, "run_id": run.id, "error_code": run.error_code},
        )
        session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

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
    run.assistant_content = result.content
    run.upstream_request_id = result.request_id
    run.model = result.model
    run.prompt_tokens = result.prompt_tokens
    run.completion_tokens = result.completion_tokens
    run.status = "completed"
    run.completed_at = now_utc()
    trigger.status = "completed"
    trigger.completed_at = now_utc()

    revision: int | None = None
    try:
        runtime_state = await request.app.state.dnd_runtime.get_campaign(
            campaign_id=campaign_id, principal_id=user.principal_id
        )
        state = runtime_state.get("result", runtime_state)
        revision = int(state.get("revision") or state.get("campaign_revision") or 0) or None
        campaign = session.get(CampaignProjection, campaign_id)
        if campaign is not None and revision is not None:
            campaign.mcp_revision = revision
    except RuntimeError:
        pass

    assistant = _append_message(
        session,
        room,
        campaign_id=campaign_id,
        sender_type="agent",
        sender_display_name="SagaSmith",
        message_type="narration",
        audience=trigger.audience,
        audience_user_ids=trigger.audience_user_ids,
        content=result.content,
        client_message_id=f"agent:{run.id}",
        trigger_message_id=trigger.id,
        structured_payload={"run_id": run.id, "model": result.model, "tokens": actual},
        mcp_revision=revision,
    )
    _emit(
        session,
        room,
        "agent.completed",
        {"message_id": trigger.id, "agent_message_id": assistant.id, "run_id": run.id},
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
            details={"campaign_id": campaign_id, "tokens": actual, "run_id": run.id},
        )
    )
    session.commit()
    return assistant


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
        response: dict[str, Any] = {"message": _message_view(existing), "agent_message": None}
        assistant = session.scalar(
            select(CampaignMessage).where(CampaignMessage.trigger_message_id == existing.id)
        )
        if assistant is not None and _message_visible(assistant, membership, user.id):
            response["agent_message"] = _message_view(assistant)
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
    assistant = None
    if payload.mode == "action":
        assistant = await _run_agent(
            request=request,
            session=session,
            campaign_id=campaign_id,
            room=room,
            membership=membership,
            user=user,
            trigger=message,
            idempotency_key=idempotency_key,
        )
    return {
        "message": _message_view(message),
        "agent_message": _message_view(assistant) if assistant is not None else None,
    }


@router.get("", response_model=CampaignRoomView)
def get_room(campaign_id: str, user: CurrentUser, session: DbSession) -> CampaignRoomView:
    _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
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
        CampaignMessageView.model_validate(item)
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
        CampaignMessageView.model_validate(item)
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
                    message_id = payload.get("message_id")
                    if message_id:
                        message = poll.get(CampaignMessage, str(message_id))
                        if message is None or not _message_visible(message, membership, user.id):
                            continue
                        payload["message"] = _message_view(message)
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
    runtime: DndRuntime = request.app.state.dnd_runtime
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
    runtime: DndRuntime = request.app.state.dnd_runtime
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
        return {"message": _message_view(existing), "receipt": existing.mcp_receipt}
    if membership.role not in {"owner", "dm"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "DM role required")
    runtime: DndRuntime = request.app.state.dnd_runtime
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
    return {"message": _message_view(message), "receipt": receipt}
