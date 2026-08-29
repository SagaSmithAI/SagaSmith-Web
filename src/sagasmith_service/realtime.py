from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from sagasmith_service.models import (
    ActorBindingProjection,
    CampaignMembershipProjection,
    CampaignProjection,
    CampaignRoomEvent,
    ModuleProject,
    ModuleRun,
    OutboxEvent,
    now_utc,
)
from sagasmith_service.observability import (
    OUTBOX_DELIVERIES,
    OUTBOX_PENDING,
    REALTIME_SUBSCRIPTIONS,
)

logger = logging.getLogger("sagasmith_service.realtime")
_REDIS_CHANNEL = "sagasmith:realtime:v1"


@dataclass(frozen=True)
class RealtimeNotice:
    event_id: str
    event_type: str
    aggregate_id: str
    topics: tuple[str, ...]
    payload: dict[str, Any]

    @classmethod
    def from_outbox(cls, item: dict[str, Any]) -> RealtimeNotice:
        payload = dict(item.get("payload") or {})
        topics = tuple(str(value) for value in payload.pop("topics", []) if value)
        return cls(
            event_id=str(item["id"]),
            event_type=str(item["event_type"]),
            aggregate_id=str(item["aggregate_id"]),
            topics=topics,
            payload=payload,
        )


class _LocalFanout:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, set[asyncio.Queue[RealtimeNotice]]] = defaultdict(set)

    @contextlib.asynccontextmanager
    async def subscribe(
        self, topics: Iterable[str], *, queue_size: int = 128
    ) -> AsyncIterator[asyncio.Queue[RealtimeNotice]]:
        topic_set = {topic for topic in topics if topic}
        queue: asyncio.Queue[RealtimeNotice] = asyncio.Queue(maxsize=queue_size)
        async with self._lock:
            for topic in topic_set:
                self._subscribers[topic].add(queue)
        REALTIME_SUBSCRIPTIONS.inc()
        try:
            yield queue
        finally:
            REALTIME_SUBSCRIPTIONS.dec()
            async with self._lock:
                for topic in topic_set:
                    subscribers = self._subscribers.get(topic)
                    if subscribers is None:
                        continue
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(topic, None)

    async def publish(self, notice: RealtimeNotice) -> None:
        async with self._lock:
            queues = {
                queue
                for topic in notice.topics
                for queue in self._subscribers.get(topic, ())
            }
        for queue in queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(notice)


class RealtimeHub:
    """Process-local fanout backed by one Redis subscription in hosted deployments."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._local = _LocalFanout()
        self._redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        self._listener: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._redis is None:
            self._ready.set()
            return
        await self._redis.ping()
        self._listener = asyncio.create_task(self._listen(), name="realtime-redis-listener")
        await asyncio.wait_for(self._ready.wait(), timeout=5)

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
        if self._redis is not None:
            await self._redis.aclose()

    @contextlib.asynccontextmanager
    async def subscribe(
        self, topics: Iterable[str]
    ) -> AsyncIterator[asyncio.Queue[RealtimeNotice]]:
        async with self._local.subscribe(topics) as queue:
            yield queue

    async def publish(self, notice: RealtimeNotice) -> None:
        if not notice.topics:
            return
        if self._redis is None:
            await self._local.publish(notice)
            return
        await self._redis.publish(
            _REDIS_CHANNEL,
            json.dumps(asdict(notice), ensure_ascii=False, separators=(",", ":")),
        )

    async def _listen(self) -> None:
        assert self._redis is not None
        async with self._redis.pubsub(ignore_subscribe_messages=True) as pubsub:
            await pubsub.subscribe(_REDIS_CHANNEL)
            self._ready.set()
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    raw = json.loads(str(message["data"]))
                    notice = RealtimeNotice(
                        event_id=str(raw["event_id"]),
                        event_type=str(raw["event_type"]),
                        aggregate_id=str(raw["aggregate_id"]),
                        topics=tuple(str(topic) for topic in raw.get("topics", [])),
                        payload=dict(raw.get("payload") or {}),
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("discarding malformed realtime notice")
                    continue
                await self._local.publish(notice)


class OutboxDispatcher:
    """Publishes committed outbox rows; duplicates are harmless wake-up signals."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        hub: RealtimeHub,
        *,
        poll_seconds: float = 0.2,
        batch_size: int = 100,
    ) -> None:
        self._factory = factory
        self._hub = hub
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="transactional-outbox")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    def _load(self) -> list[dict[str, Any]]:
        with self._factory() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "pending",
                    OutboxEvent.available_at <= now_utc(),
                    OutboxEvent.idempotency_key.like("realtime:%"),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(self._batch_size)
            ).all()
            return [
                {
                    "id": item.id,
                    "event_type": item.event_type,
                    "aggregate_id": item.aggregate_id,
                    "payload": dict(item.payload or {}),
                }
                for item in rows
            ]

    def _complete(self, event_id: str) -> None:
        with self._factory() as session:
            item = session.get(OutboxEvent, event_id)
            if item is not None and item.status == "pending":
                item.status = "completed"
                item.completed_at = now_utc()
                item.last_error = None
                session.commit()

    def _retry(self, event_id: str, error: BaseException) -> None:
        with self._factory() as session:
            item = session.get(OutboxEvent, event_id)
            if item is None or item.status != "pending":
                return
            item.attempts += 1
            item.last_error = f"{type(error).__name__}: {error}"[:1000]
            item.available_at = now_utc() + timedelta(
                seconds=min(30, 2 ** min(item.attempts, 5))
            )
            session.commit()

    async def _run(self) -> None:
        while not self._stop.is_set():
            rows = await asyncio.to_thread(self._load)
            OUTBOX_PENDING.set(len(rows))
            for item in rows:
                try:
                    await self._hub.publish(RealtimeNotice.from_outbox(item))
                    await asyncio.to_thread(self._complete, str(item["id"]))
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    OUTBOX_DELIVERIES.labels("error").inc()
                    await asyncio.to_thread(self._retry, str(item["id"]), exc)
                    logger.warning(
                        "outbox publish failed event_type=%s error_type=%s",
                        item["event_type"],
                        type(exc).__name__,
                    )
                else:
                    OUTBOX_DELIVERIES.labels("success").inc()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass


def _changed(instance: object, names: set[str]) -> bool:
    state = inspect(instance)
    return any(state.attrs[name].history.has_changes() for name in names)


def _outbox_for_flush(session: Session, _flush_context: object, _instances: object) -> None:
    entries: list[tuple[str, str, str, dict[str, Any]]] = []
    candidates = tuple(session.new) + tuple(session.dirty)
    membership_scope_changes: set[tuple[str, str]] = set()
    binding_scope_changes: set[tuple[str, str]] = set()
    for item in candidates:
        is_new = item in session.new
        if isinstance(item, CampaignRoomEvent) and is_new:
            projection_receipt: dict[str, Any] = {}
            if item.event_type == "state.changed":
                source = dict(item.payload or {})
                projection_receipt = {
                    "authority_revision": source.get("authority_revision"),
                    "changed_scopes": list(source.get("changed_scopes") or []),
                    "entity_ids": list(source.get("entity_ids") or []),
                    "audience": dict(source.get("audience") or {}),
                }
            entries.append(
                (
                    item.event_type,
                    "room",
                    item.room_id,
                    {
                        "topics": [f"room:{item.room_id}"],
                        "sequence": item.sequence,
                        **projection_receipt,
                    },
                )
            )
        elif isinstance(item, CampaignProjection) and (
            is_new or _changed(item, {"mcp_revision", "status", "visibility"})
        ):
            entries.append(
                (
                    "campaign.projection.changed",
                    "campaign",
                    item.id,
                    {
                        "topics": [f"campaign:{item.id}"],
                        "revision": item.mcp_revision,
                        "authority_revision": item.mcp_revision,
                        "changed_scopes": ["campaign"],
                        "entity_ids": [item.id],
                        "audience": {"kind": "campaign", "user_ids": []},
                    },
                )
            )
        elif isinstance(item, CampaignMembershipProjection) and (
            is_new or _changed(item, {"role", "status", "mcp_receipt"})
        ):
            membership_scope_changes.add((item.campaign_id, item.user_id))
            item.authorization_epoch = max(1, int(item.authorization_epoch or 0))
            if not is_new:
                item.authorization_epoch += 1
            entries.append(
                (
                    "access.changed",
                    "campaign",
                    item.campaign_id,
                    {
                        "topics": [
                            f"campaign:{item.campaign_id}",
                            f"principal:{item.user_id}",
                        ],
                        "user_id": item.user_id,
                        "status": item.status,
                        "authorization_epoch": item.authorization_epoch,
                        "audience_key": f"user:{item.user_id}",
                    },
                )
            )
        elif isinstance(item, ActorBindingProjection) and (
            is_new
            or _changed(
                item,
                {"user_id", "can_control", "can_view_private", "status", "mcp_receipt"},
            )
        ):
            binding_scope_changes.add((item.campaign_id, item.user_id))
            entries.append(
                (
                    "actor.binding.changed",
                    "campaign",
                    item.campaign_id,
                    {
                        "topics": [
                            f"campaign:{item.campaign_id}",
                            f"principal:{item.user_id}",
                        ],
                        "user_id": item.user_id,
                        "audience_key": f"user:{item.user_id}",
                    },
                )
            )
        elif isinstance(item, ModuleRun) and (
            is_new or _changed(item, {"status", "cancel_requested", "available_at"})
        ):
            if item.id is None:
                item.id = str(uuid.uuid4())
            entries.append(
                (
                    "module.run.changed",
                    "module_project",
                    item.project_id,
                    {"topics": [f"module:{item.project_id}"], "run_id": item.id},
                )
            )
        elif isinstance(item, ModuleProject) and (
            is_new or _changed(item, {"status", "cancel_requested", "outline_revision"})
        ):
            if item.id is None:
                item.id = str(uuid.uuid4())
            entries.append(
                (
                    "module.project.changed",
                    "module_project",
                    item.id,
                    {"topics": [f"module:{item.id}"]},
                )
            )

    memberships = {
        (item.campaign_id, item.user_id): item
        for item in candidates
        if isinstance(item, CampaignMembershipProjection)
    }
    for campaign_id, user_id in binding_scope_changes - membership_scope_changes:
        membership = memberships.get((campaign_id, user_id))
        if membership is None:
            with session.no_autoflush:
                membership = session.scalar(
                    select(CampaignMembershipProjection).where(
                        CampaignMembershipProjection.campaign_id == campaign_id,
                        CampaignMembershipProjection.user_id == user_id,
                    )
                )
        if membership is not None:
            membership.authorization_epoch = max(
                1, int(membership.authorization_epoch or 0) + 1
            )

    for event_type, aggregate_type, aggregate_id, payload in entries:
        event_id = str(uuid.uuid4())
        session.add(
            OutboxEvent(
                id=event_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                idempotency_key=f"realtime:{event_id}",
            )
        )


def install_transactional_outbox() -> None:
    if not event.contains(Session, "before_flush", _outbox_for_flush):
        event.listen(Session, "before_flush", _outbox_for_flush)
