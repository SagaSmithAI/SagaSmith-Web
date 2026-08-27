from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, TypeVar, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from sagasmith_service.api.dependencies import (
    AsyncCurrentUser,
    AsyncDbSession,
    CurrentUser,
    DbSession,
)
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
    CampaignSuggestion,
    IdentityCampaignAssignment,
    IdentityMemoryEntry,
    User,
    now_utc,
)
from sagasmith_service.observability import (
    ROOM_PROJECTION_BATCH_SECONDS,
    ROOM_PROJECTION_JOBS,
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

_ROOM_PROJECTION_CONCURRENCY = 16
_JobT = TypeVar("_JobT")
_ResultT = TypeVar("_ResultT")


async def _bounded_map_ordered(
    jobs: list[_JobT],
    worker: Callable[[_JobT], Awaitable[_ResultT]],
    *,
    limit: int = _ROOM_PROJECTION_CONCURRENCY,
) -> list[_ResultT]:
    """Run independent projection reads concurrently without changing their order."""

    if limit < 1:
        raise ValueError("projection concurrency must be positive")
    next_index = 0
    results: list[Any] = [None] * len(jobs)

    async def run() -> None:
        nonlocal next_index
        while next_index < len(jobs):
            index = next_index
            next_index += 1
            results[index] = await worker(jobs[index])

    tasks = [asyncio.create_task(run()) for _ in range(min(limit, len(jobs)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return cast(list[_ResultT], results)


async def _observed_projection_batch(
    jobs: list[_JobT],
    worker: Callable[[_JobT], Awaitable[_ResultT]],
    *,
    system: str,
    operation_class: str,
) -> list[_ResultT]:
    """Record one bounded batch without adding per-campaign metric labels."""

    transport = "streamable_http" if system in {"dnd5e", "coc7e"} else "http"
    started = time.perf_counter()
    metric_status = "success"
    try:
        return await _bounded_map_ordered(jobs, worker)
    except BaseException:
        metric_status = "error"
        raise
    finally:
        labels = {
            "system": system,
            "operation_class": operation_class,
            "status": metric_status,
            "transport": transport,
        }
        ROOM_PROJECTION_BATCH_SECONDS.labels(**labels).observe(
            time.perf_counter() - started
        )
        ROOM_PROJECTION_JOBS.labels(**labels).observe(len(jobs))


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
    return _message_visible_for_role(message, membership.role, user_id)


def _message_visible_for_role(
    message: CampaignMessage,
    membership_role: str,
    user_id: str,
) -> bool:
    if message.audience == "public":
        return True
    if message.sender_user_id == user_id:
        return True
    if message.audience == "dm":
        return membership_role in {"owner", "dm"}
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


def _runtime_for_system(request: Request, system_id: str) -> Any:
    runtimes = getattr(request.app.state, "game_runtimes", {})
    runtime = runtimes.get(system_id) if isinstance(runtimes, dict) else None
    if runtime is None and system_id == "dnd5e":
        runtime = getattr(request.app.state, "dnd_runtime", None)
    if runtime is None:
        raise ValueError(f"no hosted runtime for system {system_id!r}")
    return runtime


def _campaign_runtime(request: Request, session: Session, campaign_id: str) -> Any:
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise ValueError("campaign projection not found")
    return _runtime_for_system(request, campaign.system_id)


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


@dataclass(frozen=True)
class _RoomProjectionContext:
    """One request-scoped snapshot for projection authorization and routing."""

    campaign: CampaignProjection
    runtime: Any
    trigger: CampaignMessage
    members_by_user: dict[str, CampaignMembershipProjection]
    users_by_id: dict[str, User]
    bindings_by_actor: dict[str, tuple[ActorBindingProjection, ...]]

    @classmethod
    def load(
        cls,
        request: Request,
        session: Session,
        *,
        campaign_id: str,
        trigger: CampaignMessage,
        campaign: CampaignProjection | None = None,
    ) -> _RoomProjectionContext:
        campaign = campaign or session.get(CampaignProjection, campaign_id)
        if campaign is None:
            raise ValueError("campaign projection not found")
        members = session.scalars(
            select(CampaignMembershipProjection).where(
                CampaignMembershipProjection.campaign_id == campaign_id,
                CampaignMembershipProjection.status == "active",
            )
        ).all()
        members_by_user = {item.user_id: item for item in members}
        users = (
            session.scalars(
                select(User).where(User.id.in_(sorted(members_by_user)))
            ).all()
            if members_by_user
            else []
        )
        users_by_id = {item.id: item for item in users}
        if users_by_id.keys() != members_by_user.keys():
            raise ValueError("campaign membership contains an unknown user")
        bindings = session.scalars(
            select(ActorBindingProjection).where(
                ActorBindingProjection.campaign_id == campaign_id,
                ActorBindingProjection.status == "active",
            )
        ).all()
        bindings_by_actor: dict[str, list[ActorBindingProjection]] = {}
        for binding in bindings:
            bindings_by_actor.setdefault(binding.actor_id, []).append(binding)
        return cls(
            campaign=campaign,
            runtime=_campaign_runtime(request, session, campaign_id),
            trigger=trigger,
            members_by_user=members_by_user,
            users_by_id=users_by_id,
            bindings_by_actor={
                actor_id: tuple(sorted(items, key=lambda item: item.id))
                for actor_id, items in bindings_by_actor.items()
            },
        )

    def audience_users(
        self,
        audience: str,
        audience_user_ids: list[str],
    ) -> set[str]:
        if audience == "public":
            return set(self.members_by_user)
        if audience == "dm":
            return {
                user_id
                for user_id, membership in self.members_by_user.items()
                if membership.role in {"owner", "dm"}
            }
        requested = set(audience_user_ids)
        return set(self.members_by_user).intersection(requested)

    def resolve_audience(self, requested: RoomAudience) -> tuple[str, list[str]]:
        if requested.kind == "public":
            audience, user_ids = "public", []
        elif requested.kind == "dm":
            audience, user_ids = "dm", []
        else:
            bindings = [
                binding
                for actor_ref in requested.actor_refs
                for binding in self.bindings_by_actor.get(actor_ref, ())
                if binding.can_view_private
            ]
            bound_refs = {item.actor_id for item in bindings}
            if bound_refs != set(requested.actor_refs):
                raise ValueError("actor audience is not fully bound")
            user_ids = sorted({item.user_id for item in bindings})
            if not user_ids:
                raise ValueError("actor audience has no active recipients")
            audience = "private"
        trigger_users = self.audience_users(
            self.trigger.audience,
            list(self.trigger.audience_user_ids or []),
        )
        output_users = self.audience_users(audience, user_ids)
        if not output_users or not output_users.issubset(trigger_users):
            raise ValueError("structured output would broaden the trigger audience")
        return audience, user_ids

    def principals_for(self, user_ids: set[str], *, subject: str) -> list[str]:
        missing = user_ids.difference(self.users_by_id)
        if missing:
            raise ValueError(f"{subject} audience contains an unknown user")
        return [self.users_by_id[user_id].principal_id for user_id in sorted(user_ids)]


@dataclass(frozen=True)
class _ProjectionJob:
    kind: str
    key: tuple[str, str]
    principal_id: str
    suggestion: bool = False


async def _projection_indexes(
    context: _RoomProjectionContext,
    *,
    submission: RoomTurnSubmission,
) -> tuple[
    dict[str, tuple[str, list[str]]],
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    """Plan every independent projection and execute one globally bounded queue."""

    resolution_groups: dict[tuple[str, str], list[str]] = {}
    actor_groups: dict[tuple[str, str], list[str]] = {}
    audiences: dict[str, tuple[str, list[str]]] = {}
    resolution_jobs: list[_ProjectionJob] = []
    actor_jobs: list[_ProjectionJob] = []
    for output in submission.messages:
        audience, audience_user_ids = context.resolve_audience(output.audience)
        audiences[output.output_id] = (audience, audience_user_ids)
        target_user_ids = context.audience_users(audience, audience_user_ids)
        resolution_principals = context.principals_for(
            target_user_ids,
            subject="resolution",
        )
        actor_principals = context.principals_for(
            target_user_ids,
            subject="actor presentation",
        )
        for resolution_id in sorted(_output_resolution_ids(output)):
            key = (output.output_id, resolution_id)
            resolution_groups[key] = resolution_principals
            resolution_jobs.extend(
                _ProjectionJob("resolution", key, principal_id)
                for principal_id in resolution_principals
            )
        actor_refs = {
            str(block.speaker.actor_ref)
            for block in output.blocks
            if isinstance(block, PerformanceBlock)
            and block.speaker.kind == "published_actor"
            and block.speaker.actor_ref
        }
        for actor_ref in sorted(actor_refs):
            key = (output.output_id, actor_ref)
            actor_groups[key] = actor_principals
            actor_jobs.extend(
                _ProjectionJob("actor", key, principal_id)
                for principal_id in actor_principals
            )

    suggestion_actor_refs = {
        str(item.actor_ref) for item in submission.suggestions if item.actor_ref
    }
    target = (
        context.users_by_id.get(context.trigger.sender_user_id)
        if context.trigger.sender_user_id
        else None
    )
    if suggestion_actor_refs and target is None:
        raise ValueError("suggestion actor has no triggering principal")
    suggestion_keys = [
        ("__suggestion__", actor_ref)
        for actor_ref in sorted(suggestion_actor_refs)
    ]
    actor_jobs.extend(
        _ProjectionJob("actor", key, target.principal_id, suggestion=True)
        for key in suggestion_keys
        if target is not None
    )

    jobs: list[_ProjectionJob] = []
    for index in range(max(len(resolution_jobs), len(actor_jobs), 0)):
        if index < len(resolution_jobs):
            jobs.append(resolution_jobs[index])
        if index < len(actor_jobs):
            jobs.append(actor_jobs[index])

    async def fetch(job: _ProjectionJob) -> tuple[_ProjectionJob, dict[str, Any]]:
        _, reference_id = job.key
        if job.kind == "resolution":
            try:
                projection = await context.runtime.get_resolution_presentation(
                    campaign_id=context.campaign.id,
                    resolution_id=reference_id,
                    principal_id=job.principal_id,
                )
            except RuntimeError as exc:
                raise ValueError(
                    "resolution is not visible to the complete message audience"
                ) from exc
            if (
                projection.get("schema") != "sagasmith.resolution-presentation/v1"
                or str(projection.get("resolution_id") or "") != reference_id
                or str(projection.get("campaign_id") or "") != context.campaign.id
                or str(projection.get("system_id") or "") != context.campaign.system_id
            ):
                raise ValueError("MCP returned a mismatched resolution presentation")
            return job, dict(projection)
        try:
            actor = await context.runtime.get_character_card(
                campaign_id=context.campaign.id,
                character_id=reference_id,
                principal_id=job.principal_id,
            )
        except RuntimeError as exc:
            message = (
                "suggestion actor is not visible to its target"
                if job.suggestion
                else "published actor is not visible to the message audience"
            )
            raise ValueError(message) from exc
        if (
            str(actor.get("id") or "") != reference_id
            or str(actor.get("campaign_id") or "") != context.campaign.id
            or (not job.suggestion and not str(actor.get("name") or "").strip())
        ):
            message = (
                "MCP returned a mismatched suggestion actor"
                if job.suggestion
                else "MCP returned a mismatched actor presentation"
            )
            raise ValueError(message)
        return job, {
            "label": str(actor.get("name") or ""),
            "character_type": str(actor.get("character_type") or ""),
            "revision": int(actor.get("revision") or 0),
        }

    resolution_values = {key: [] for key in resolution_groups}
    actor_values = {key: [] for key in actor_groups}
    suggestion_values = {key: [] for key in suggestion_keys}
    results = await _observed_projection_batch(
        jobs,
        fetch,
        system=context.campaign.system_id,
        operation_class="turn",
    )
    for job, value in results:
        if job.kind == "resolution":
            resolution_values[job.key].append(value)
        elif job.suggestion:
            suggestion_values[job.key].append(value)
        else:
            actor_values[job.key].append(value)

    resolution_index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, values in resolution_values.items():
        if not values:
            raise ValueError("resolution message has no audience")
        comparison = {
            json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for item in values
        }
        if len(comparison) != 1:
            raise ValueError("MCP resolution projection differs inside one message audience")
        resolution_index[key] = values[0]

    actor_index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, values in actor_values.items():
        if not values or len(
            {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in values}
        ) != 1:
            raise ValueError("actor presentation differs inside one message audience")
        actor_index[key] = values[0]
    for key, values in suggestion_values.items():
        if not values:
            raise ValueError("suggestion actor lacks an MCP presentation")
        actor_index[key] = values[0]
    return audiences, resolution_index, actor_index


def _emit(
    session: Session,
    room: CampaignRoom,
    event_type: str,
    payload: dict[str, Any],
) -> CampaignRoomEvent:
    sequence = room.next_event_sequence
    room.next_event_sequence += 1
    run_id = str(payload.get("run_id") or "") or None
    activity_id = None
    activity_state = None
    if event_type == "room.activity":
        activity_id = str(payload.get("activity_id") or "") or None
        activity_state = str(payload.get("state") or "") or None
    event = CampaignRoomEvent(
        room_id=room.id,
        sequence=sequence,
        event_type=event_type,
        run_id=run_id,
        activity_id=activity_id,
        activity_state=activity_state,
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
    latest = (
        select(
            CampaignRoomEvent.activity_id.label("activity_id"),
            func.max(CampaignRoomEvent.sequence).label("sequence"),
        )
        .where(
            CampaignRoomEvent.room_id == room.id,
            CampaignRoomEvent.run_id == run_id,
            CampaignRoomEvent.activity_id.is_not(None),
        )
        .group_by(CampaignRoomEvent.activity_id)
        .subquery()
    )
    events = session.execute(
        select(
            CampaignRoomEvent.activity_id,
            CampaignRoomEvent.activity_state,
            CampaignRoomEvent.payload,
        )
        .join(
            latest,
            (CampaignRoomEvent.activity_id == latest.c.activity_id)
            & (CampaignRoomEvent.sequence == latest.c.sequence),
        )
        .where(
            CampaignRoomEvent.room_id == room.id,
            CampaignRoomEvent.run_id == run_id,
        )
    ).all()
    for activity_id, activity_state, raw_payload in events:
        payload = dict(raw_payload or {})
        payload["state"] = activity_state
        if not activity_id or activity_state != "started":
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
    statement = select(CampaignSuggestion).where(
        CampaignSuggestion.room_id == room.id,
        CampaignSuggestion.expired.is_(False),
    )
    if target_user_id is not None:
        statement = statement.where(CampaignSuggestion.target_user_id == target_user_id)
    suggestions_to_expire = session.scalars(statement.with_for_update()).all()
    by_message: dict[str, set[tuple[str, str | None]]] = {}
    for suggestion_row in suggestions_to_expire:
        suggestion_row.expired = True
        suggestion_payload = deepcopy(dict(suggestion_row.payload or {}))
        valid_for = dict(suggestion_payload.get("valid_for") or {})
        valid_for["expired"] = True
        suggestion_payload["valid_for"] = valid_for
        suggestion_row.payload = suggestion_payload
        by_message.setdefault(suggestion_row.message_id, set()).add(
            (suggestion_row.suggestion_id, suggestion_row.target_user_id)
        )
    if not by_message:
        return
    messages = session.scalars(
        select(CampaignMessage).where(CampaignMessage.id.in_(by_message))
    ).all()
    for message in messages:
        payload = deepcopy(dict(message.structured_payload or {}))
        suggestions = list(payload.get("suggestions") or [])
        expiring = by_message[message.id]
        changed = False
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            key = (str(suggestion.get("id") or ""), suggestion.get("target_user_id"))
            if key not in expiring:
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


def _index_message_suggestions(session: Session, message: CampaignMessage) -> None:
    if message.sender_type != "agent":
        return
    message_payload = dict(message.structured_payload or {})
    if message_payload.get("schema") != "sagasmith.room-message/v1":
        return
    for raw_suggestion in list(message_payload.get("suggestions") or []):
        if not isinstance(raw_suggestion, dict):
            continue
        suggestion_id = str(raw_suggestion.get("id") or "")
        target_user_id = str(raw_suggestion.get("target_user_id") or "") or None
        valid_for = dict(raw_suggestion.get("valid_for") or {})
        run_id = str(valid_for.get("run_id") or message_payload.get("run_id") or "")
        if not suggestion_id or target_user_id is None or not run_id:
            continue
        session.add(
            CampaignSuggestion(
                room_id=message.room_id,
                message_id=message.id,
                suggestion_id=suggestion_id,
                target_user_id=target_user_id,
                actor_ref=str(raw_suggestion.get("actor_ref") or "") or None,
                run_id=run_id,
                expired=bool(valid_for.get("expired", False)),
                valid_revision=(
                    int(valid_for["revision"])
                    if valid_for.get("revision") is not None
                    else None
                ),
                valid_phase=str(valid_for.get("phase") or "") or None,
                payload=deepcopy(raw_suggestion),
            )
        )


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
    _index_message_suggestions(session, item)
    _emit(session, room, "message.created", {"message_id": item.id})
    return item


def _recent_context(
    session: Session,
    room: CampaignRoom,
    membership_role: str,
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
    visible = [
        item
        for item in reversed(candidates)
        if _message_visible_for_role(item, membership_role, user_id)
    ]
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
    *,
    context: _RoomProjectionContext,
    submission: RoomTurnSubmission,
    run_id: str,
    host_role: str,
    user: User,
    secret: Any,
    revision: int | None,
    phase: str | None,
    audiences: dict[str, tuple[str, list[str]]],
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
            controls_actor = any(
                binding.user_id == user.id and binding.can_control
                for binding in context.bindings_by_actor.get(suggestion.actor_ref, ())
            )
            if not controls_actor:
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
                    "trigger_sequence": context.trigger.sequence,
                    "expired": False,
                },
            }
        )

    suggestions_attached = False
    for output in submission.messages:
        audience, audience_user_ids = audiences[output.output_id]
        blocks: list[dict[str, Any]] = []
        for block in output.blocks:
            value = block.model_dump(mode="json")
            if isinstance(block, PerformanceBlock):
                actor_ref = block.speaker.actor_ref
                bindings = context.bindings_by_actor.get(actor_ref, ()) if actor_ref else ()
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
                        or provenance.source_message_id != context.trigger.id
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
                    campaign_id=context.campaign.id,
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

        output_users = context.audience_users(audience, audience_user_ids)
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


@dataclass(frozen=True)
class _MessagePreparation:
    message_id: str
    response: dict[str, Any] | None
    run_agent: bool
    membership_role: str


@dataclass(frozen=True)
class _AgentPreparation:
    campaign: CampaignProjection
    campaign_system_id: str
    domain_runtime: Any
    principal_id: str
    host_role: str
    viewer_role: str
    reservation_id: str
    reservation_quantity: Decimal
    run_id: str
    room_id: str
    trigger_id: str
    trigger_content: str
    trigger_payload: dict[str, Any]
    sender_display_name: str
    session_id: str
    identity_context: dict[str, Any]
    room_context: list[dict[str, Any]]


def _prepare_message_transaction(
    session: Session,
    *,
    campaign_id: str,
    payload: CampaignMessageCreate,
    user_id: str,
    user_display_name: str,
    idempotency_key: str,
) -> _MessagePreparation:
    membership = _membership(session, campaign_id, user_id)
    room = _room(session, campaign_id)
    existing = session.scalar(
        select(CampaignMessage).where(
            CampaignMessage.room_id == room.id,
            CampaignMessage.client_message_id == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.sender_user_id != user_id
            or existing.content != payload.content
            or existing.message_type != payload.mode
            or existing.audience != payload.audience
            or list(existing.audience_user_ids or []) != payload.audience_user_ids
            or existing.reply_to_message_id != payload.reply_to_message_id
            or dict(existing.structured_payload or {}) != payload.structured_payload
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key payload mismatch")
        assistants = session.scalars(
            select(CampaignMessage)
            .where(CampaignMessage.trigger_message_id == existing.id)
            .order_by(CampaignMessage.sequence)
        ).all()
        visible_assistant = next(
            (item for item in assistants if _message_visible(item, membership, user_id)),
            None,
        )
        response = {
            "message": _message_view(existing, user_id),
            "agent_message": (
                _message_view(visible_assistant, user_id)
                if visible_assistant is not None
                else None
            ),
        }
        session.commit()
        return _MessagePreparation(existing.id, response, False, membership.role)
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
            parent, membership, user_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "reply target not found")
    if payload.mode == "action":
        _expire_suggestions(session, room, target_user_id=user_id)
    message = _append_message(
        session,
        room,
        campaign_id=campaign_id,
        sender_type="user",
        sender_user_id=user_id,
        sender_display_name=user_display_name,
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
            actor_user_id=user_id,
            action="campaign.room.message.create",
            subject_type="campaign_message",
            subject_id=message.id,
            details={"campaign_id": campaign_id, "mode": payload.mode},
        )
    )
    session.commit()
    response = None
    if payload.mode != "action":
        response = {"message": _message_view(message, user_id), "agent_message": None}
    return _MessagePreparation(
        message.id,
        response,
        payload.mode == "action",
        membership.role,
    )


def _prepare_agent_transaction(
    session: Session,
    *,
    request: Request,
    campaign_id: str,
    trigger_id: str,
    user_id: str,
    user_principal_id: str,
    viewer_role: str,
    idempotency_key: str,
) -> _AgentPreparation:
    campaign = session.get(CampaignProjection, campaign_id)
    trigger = session.get(CampaignMessage, trigger_id)
    if campaign is None or trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign action context not found")
    room = _room(session, campaign_id)
    principal_id = user_principal_id
    host_role = viewer_role
    quota_user_id = user_id
    conversation_user_id = user_id
    identity_assignment_id: str | None = None
    identity_id: str | None = None
    identity_context: dict[str, Any] = {}
    sender_display_name = "SagaSmith"
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
        identity_id = identity.id
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
        else None
    )
    context_user_id = conversation_user_id if identity_assignment_id is not None else user_id
    reservation_quantity = Decimal(request.app.state.settings.agent_reservation_tokens)
    try:
        reservation = reserve(
            session,
            user_id=quota_user_id,
            campaign_id=campaign_id,
            metric="llm_tokens",
            quantity=reservation_quantity,
            idempotency_key=f"room-agent-reserve:{user_id}:{idempotency_key}",
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
        user_id=user_id,
        idempotency_key=f"room:{idempotency_key}",
        request_hash=hashlib.sha256(trigger.content.encode()).hexdigest(),
        user_content=trigger.content,
    )
    session.add(run)
    session.flush()
    room_context = _recent_context(
        session,
        room,
        context_membership.role if context_membership is not None else viewer_role,
        context_user_id,
    )
    _emit(session, room, "agent.started", {"message_id": trigger.id, "run_id": run.id})
    domain_runtime = _campaign_runtime(request, session, campaign_id)
    session.commit()
    session_id = (
        f"{campaign_id}:agent:{identity_id}:{conversation.id}"
        if identity_assignment_id is not None
        else f"{campaign_id}:{user_id}:{conversation.id}"
    )
    return _AgentPreparation(
        campaign=campaign,
        campaign_system_id=campaign.system_id,
        domain_runtime=domain_runtime,
        principal_id=principal_id,
        host_role=host_role,
        viewer_role=viewer_role,
        reservation_id=reservation.id,
        reservation_quantity=reservation_quantity,
        run_id=run.id,
        room_id=room.id,
        trigger_id=trigger.id,
        trigger_content=trigger.content,
        trigger_payload=dict(trigger.structured_payload or {}),
        sender_display_name=sender_display_name,
        session_id=session_id,
        identity_context=identity_context,
        room_context=room_context,
    )


def _record_agent_unavailable(
    session: Session,
    *,
    campaign_id: str,
    preparation: _AgentPreparation,
) -> None:
    room = _room(session, campaign_id)
    trigger = session.get(CampaignMessage, preparation.trigger_id)
    run = session.get(AgentRun, preparation.run_id)
    if trigger is None or run is None:
        raise RuntimeError("persisted Agent run is unavailable")
    release(session, preparation.reservation_id)
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


async def _run_agent(
    *,
    request: Request,
    session: AsyncSession,
    campaign_id: str,
    trigger_id: str,
    user: User,
    viewer_role: str,
    idempotency_key: str,
) -> dict[str, Any]:
    preparation = await session.run_sync(
        lambda sync_session: _prepare_agent_transaction(
            sync_session,
            request=request,
            campaign_id=campaign_id,
            trigger_id=trigger_id,
            user_id=user.id,
            user_principal_id=user.principal_id,
            viewer_role=viewer_role,
            idempotency_key=idempotency_key,
        )
    )
    settings = request.app.state.settings
    activity_callback = (
        f"{settings.service_internal_url.rstrip('/')}/api/campaigns/{campaign_id}"
        f"/room/internal-activity/{preparation.run_id}"
    )
    runtime: AgentRuntime = request.app.state.agent_runtime
    try:
        result = await runtime.complete(
            session_id=preparation.session_id,
            content=preparation.trigger_content,
            context={
                "campaign_id": campaign_id,
                "system_id": preparation.campaign_system_id,
                "principal_id": preparation.principal_id,
                "campaign_role": preparation.host_role,
                "room_id": preparation.room_id,
                "room_context": preparation.room_context,
                "action_context": preparation.trigger_payload,
                "run_id": preparation.run_id,
                "trigger_message_id": preparation.trigger_id,
                "response_contract": {
                    "terminal": room_turn_contract(),
                    "activity": room_activity_contract(),
                    "activity_callback": {
                        "url": activity_callback,
                        "token": _activity_token(
                            settings.session_secret,
                            campaign_id,
                            preparation.run_id,
                        ),
                    },
                },
                **preparation.identity_context,
            },
        )
    except RuntimeError as exc:
        await session.run_sync(
            lambda sync_session: _record_agent_unavailable(
                sync_session,
                campaign_id=campaign_id,
                preparation=preparation,
            )
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    revision: int | None = None
    phase: str | None = None
    try:
        runtime_state = await preparation.domain_runtime.get_campaign(
            campaign_id=campaign_id,
            principal_id=preparation.principal_id,
        )
        state = runtime_state.get("result", runtime_state)
        revision = int(state.get("revision") or state.get("campaign_revision") or 0) or None
        phase_value = (
            state.get("effective_game_phase")
            or state.get("phase")
            or state.get("game_phase")
        )
        phase = str(phase_value) if phase_value else None
    except RuntimeError:
        pass

    actual = min(result.total_tokens, int(preparation.reservation_quantity))
    try:
        if result.structured_output is None:
            raise ValueError("Agent returned no structured room output")
        submission = RoomTurnSubmission.model_validate(result.structured_output)
        projection_context = await session.run_sync(
            lambda sync_session: _RoomProjectionContext.load(
                request,
                sync_session,
                campaign_id=campaign_id,
                trigger=cast(
                    CampaignMessage,
                    sync_session.get(CampaignMessage, preparation.trigger_id),
                ),
                campaign=preparation.campaign,
            )
        )
        # End the read transaction before any MCP projection await. With
        # expire_on_commit=False the immutable request snapshot remains usable.
        await session.commit()
        audiences, resolution_index, actor_presentation_index = await _projection_indexes(
            projection_context,
            submission=submission,
        )
        projected = _project_turn_messages(
            context=projection_context,
            submission=submission,
            run_id=preparation.run_id,
            host_role=preparation.host_role,
            user=user,
            secret=settings.session_secret,
            revision=revision,
            phase=phase,
            audiences=audiences,
            resolution_index=resolution_index,
            actor_presentation_index=actor_presentation_index,
        )
    except (ValidationError, ValueError) as exc:
        if session.in_transaction():
            await session.rollback()

        def record_invalid(sync_session: Session) -> None:
            room = _room(sync_session, campaign_id)
            trigger = sync_session.get(CampaignMessage, preparation.trigger_id)
            run = sync_session.get(AgentRun, preparation.run_id)
            if trigger is None or run is None:
                raise RuntimeError("persisted Agent run is unavailable")
            settle(
                sync_session,
                reservation_id=preparation.reservation_id,
                quantity=Decimal(actual),
                idempotency_key=f"room-agent-settle:{user.id}:{idempotency_key}",
                unit="tokens",
                provider="nanobot",
                model=result.model,
                request_id=result.request_id,
            )
            campaign = sync_session.get(CampaignProjection, campaign_id)
            if campaign is not None and revision is not None:
                campaign.mcp_revision = revision
            trigger.status = "failed"
            trigger.completed_at = now_utc()
            run.status = "failed"
            run.error_code = "agent_invalid_output"
            run.assistant_content = result.content
            run.upstream_request_id = result.request_id
            run.model = result.model
            run.prompt_tokens = result.prompt_tokens
            run.completion_tokens = result.completion_tokens
            run.completed_at = now_utc()
            _close_run_activities(sync_session, room, run_id=run.id, state="failed")
            _emit(
                sync_session,
                room,
                "agent.failed",
                {"message_id": trigger.id, "run_id": run.id, "error_code": run.error_code},
            )
            sync_session.add(
                AuditEvent(
                    actor_user_id=user.id,
                    action="campaign.room.agent.invalid_output",
                    subject_type="campaign_message",
                    subject_id=trigger.id,
                    details={"campaign_id": campaign_id, "run_id": run.id},
                )
            )
            sync_session.commit()

        await session.run_sync(record_invalid)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Agent returned an invalid structured room response",
        ) from exc

    def complete(sync_session: Session) -> dict[str, Any]:
        room = _room(sync_session, campaign_id)
        trigger = sync_session.get(CampaignMessage, preparation.trigger_id)
        run = sync_session.get(AgentRun, preparation.run_id)
        if trigger is None or run is None:
            raise RuntimeError("persisted Agent run is unavailable")
        settle(
            sync_session,
            reservation_id=preparation.reservation_id,
            quantity=Decimal(actual),
            idempotency_key=f"room-agent-settle:{user.id}:{idempotency_key}",
            unit="tokens",
            provider="nanobot",
            model=result.model,
            request_id=result.request_id,
        )
        campaign = sync_session.get(CampaignProjection, campaign_id)
        if campaign is not None and revision is not None:
            campaign.mcp_revision = revision
        run.upstream_request_id = result.request_id
        run.model = result.model
        run.prompt_tokens = result.prompt_tokens
        run.completion_tokens = result.completion_tokens
        assistants = [
            _append_message(
                sync_session,
                room,
                campaign_id=campaign_id,
                sender_type="agent",
                sender_display_name=preparation.sender_display_name,
                message_type="presentation",
                audience=output["audience"],
                audience_user_ids=output["audience_user_ids"],
                content=output["content"],
                client_message_id=f"agent:{run.id}:{output['output_id']}",
                trigger_message_id=trigger.id,
                structured_payload=output["structured_payload"],
                mcp_revision=revision,
            )
            for output in projected
        ]
        run.assistant_content = "\n\n".join(item.content for item in assistants)
        run.status = "completed"
        run.completed_at = now_utc()
        trigger.status = "completed"
        trigger.completed_at = now_utc()
        _close_run_activities(sync_session, room, run_id=run.id, state="superseded")
        _emit(
            sync_session,
            room,
            "agent.completed",
            {
                "message_id": trigger.id,
                "agent_message_ids": [item.id for item in assistants],
                "run_id": run.id,
            },
        )
        _emit(
            sync_session,
            room,
            "state.changed",
            {"reason": "agent.completed", "mcp_revision": revision},
        )
        sync_session.add(
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
        sync_session.commit()
        visible_assistant = next(
            (
                item
                for item in assistants
                if _message_visible_for_role(item, preparation.viewer_role, user.id)
            ),
            None,
        )
        return {
            "message": _message_view(trigger, user.id),
            "agent_message": (
                _message_view(visible_assistant, user.id)
                if visible_assistant is not None
                else None
            ),
        }

    return await session.run_sync(complete)


async def _post_message(
    *,
    campaign_id: str,
    payload: CampaignMessageCreate,
    request: Request,
    user: User,
    session: AsyncSession,
    idempotency_key: str,
) -> dict[str, Any]:
    async def prepare_once() -> _MessagePreparation:
        return await session.run_sync(
            lambda sync_session: _prepare_message_transaction(
                sync_session,
                campaign_id=campaign_id,
                payload=payload,
                user_id=user.id,
                user_display_name=user.display_name,
                idempotency_key=idempotency_key,
            )
        )

    try:
        preparation = await prepare_once()
    except IntegrityError:
        # A concurrent retry or SQLite writer can race after the initial
        # idempotency read. Re-read once from a clean transaction so the
        # unique key remains the authority and a stale room sequence is retried.
        await session.rollback()
        preparation = await prepare_once()
    if preparation.response is not None:
        return preparation.response
    if not preparation.run_agent:
        raise RuntimeError("room message preparation returned no response")
    return await _run_agent(
        request=request,
        session=session,
        campaign_id=campaign_id,
        trigger_id=preparation.message_id,
        user=user,
        viewer_role=preparation.membership_role,
        idempotency_key=idempotency_key,
    )


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
    previous_state = session.scalar(
        select(CampaignRoomEvent.activity_state)
        .where(
            CampaignRoomEvent.room_id == room.id,
            CampaignRoomEvent.run_id == run_id,
            CampaignRoomEvent.activity_id == payload.activity_id,
        )
        .order_by(CampaignRoomEvent.sequence.desc())
        .limit(1)
    )
    if previous_state == payload.state:
        return {"accepted": True}
    terminal = {"completed", "failed", "cancelled", "superseded"}
    if (previous_state is None and payload.state != "started") or previous_state in terminal:
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
    user: AsyncCurrentUser,
    session: AsyncDbSession,
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
    user: AsyncCurrentUser,
    session: AsyncDbSession,
) -> dict[str, Any]:
    user_id = user.id
    principal_id = user.principal_id
    membership = await session.scalar(
        select(CampaignMembershipProjection).where(
            CampaignMembershipProjection.campaign_id == campaign_id,
            CampaignMembershipProjection.user_id == user_id,
            CampaignMembershipProjection.status == "active",
        )
    )
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "campaign membership required")
    campaign = await session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise ValueError("campaign projection not found")
    membership_role = membership.role
    runtime = _runtime_for_system(request, campaign.system_id)
    await session.rollback()
    try:
        value = await runtime.get_panel_state(campaign_id=campaign_id, principal_id=principal_id)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    bindings = (
        await session.scalars(
            select(ActorBindingProjection).where(
                ActorBindingProjection.campaign_id == campaign_id,
                ActorBindingProjection.status == "active",
            )
        )
    ).all()
    visible_bindings = (
        bindings
        if membership_role in {"owner", "dm"}
        else [item for item in bindings if item.user_id == user_id]
    )
    binding_views = [
        {
            "actor_id": item.actor_id,
            "user_id": item.user_id,
            "can_control": item.can_control,
            "can_view_private": item.can_view_private,
        }
        for item in visible_bindings
    ]
    await session.rollback()
    return {
        **value,
        "membership": {"role": membership_role, "user_id": user_id},
        "actor_bindings": binding_views,
    }


@router.get("/combat/render")
async def public_combat_render(
    campaign_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> Response:
    """Return only the D&D MCP's party-public combat projection."""

    _membership(session, campaign_id, user.id)
    campaign = session.get(CampaignProjection, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    if campaign.system_id != "dnd5e":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "combat render unavailable")
    runtime = _campaign_runtime(request, session, campaign_id)
    try:
        rendered = await runtime.render_public_combat(
            campaign_id=campaign_id,
            principal_id=user.principal_id,
        )
    except (AttributeError, RuntimeError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "public combat render unavailable"
        ) from exc
    if rendered.metadata.get("audience_projection") != "party_public":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invalid combat render projection")
    checksum = rendered.metadata["image_checksum"]
    public_text_headers = {}
    for field, header, limit in (
        ("alt_text", "X-SagaSmith-Combat-Alt", 240),
        ("suggested_caption", "X-SagaSmith-Combat-Caption", 500),
    ):
        value = rendered.metadata.get(field)
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())[:limit].strip()
        if value:
            public_text_headers[header] = base64.urlsafe_b64encode(
                value.encode("utf-8")
            ).decode("ascii").rstrip("=")
    return Response(
        content=rendered.content,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="sagasmith-party-combat.png"',
            "ETag": f'"{checksum}"',
            "X-Content-Type-Options": "nosniff",
            **public_text_headers,
        },
    )


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
    async_session: AsyncDbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> dict[str, Any]:
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
            session=async_session,
            idempotency_key=idempotency_key,
        )
    membership = _membership(session, campaign_id, user.id)
    room = _room(session, campaign_id)
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
