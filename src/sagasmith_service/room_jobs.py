from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from sagasmith_service.models import CampaignMessage, RoomTurnJob, now_utc
from sagasmith_service.observability import (
    ROOM_TURN_JOB_QUEUE,
    ROOM_TURN_JOB_RECOVERIES,
    ROOM_TURN_JOB_SECONDS,
    ROOM_TURN_JOB_TRANSITIONS,
)
from sagasmith_service.quota import (
    QuotaReservationExpiredError,
    expire_abandoned,
    release,
    renew,
)

logger = logging.getLogger("sagasmith_service.room_jobs")
TERMINAL_ROOM_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_ROOM_JOB_STATES = frozenset({"queued", "running", "waiting"})


@dataclass(frozen=True)
class RoomJobError(RuntimeError):
    code: str
    message: str
    retryable: bool = True
    error_class: str = "internal"

    def __str__(self) -> str:
        return self.message


class RoomTurnJobProcessor:
    """Lease-based in-process dispatcher over durable database jobs.

    Multiple Web replicas can run this processor because claims use row locks.
    The database row, not a process-local task, is the source of truth.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        executor: Callable[[str], Awaitable[None]],
        *,
        concurrency: int,
        poll_seconds: float,
        lease_seconds: int,
        per_room_concurrency: int,
        reservation_ttl_seconds: int,
        retry_seconds: int,
        failure_recorder: Callable[[Session, RoomTurnJob, RoomJobError], None] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.factory = factory
        self.executor = executor
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.per_room_concurrency = per_room_concurrency
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self.retry_seconds = retry_seconds
        self.failure_recorder = failure_recorder
        self.worker_id = worker_id or f"web-{socket.gethostname()}-{id(self)}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._settlement_locks: dict[str, asyncio.Lock] = {}
        self._room_execution_slots: dict[str, asyncio.Semaphore] = {}
        self._claim_lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()

    def start(self) -> None:
        if self._workers:
            return
        self._stop.clear()
        self._workers = [
            asyncio.create_task(self._worker_loop(), name=f"room-turn-worker-{index}")
            for index in range(self.concurrency)
        ]
        self.notify()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def notify(self) -> None:
        self._wake.set()

    def _transition(self, item: RoomTurnJob, status: str, reason: str) -> None:
        previous = item.status
        item.status = status
        ROOM_TURN_JOB_TRANSITIONS.labels(
            from_status=previous,
            to_status=status,
            reason=reason,
        ).inc()

    def recover_expired(self) -> int:
        now = now_utc()
        recovered = 0
        with self.factory() as session:
            rows = session.scalars(
                select(RoomTurnJob)
                .where(
                    RoomTurnJob.status.in_(("running", "waiting")),
                    RoomTurnJob.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            ).all()
            for item in rows:
                prior = item.status
                if item.cancel_requested:
                    self._transition(item, "cancelled", "lease_cancel")
                    item.completed_at = now
                    if item.reservation_id:
                        release(session, item.reservation_id)
                    if self.failure_recorder is not None:
                        self.failure_recorder(
                            session,
                            item,
                            RoomJobError("cancelled", "Room turn was cancelled", False, "cancel"),
                        )
                elif item.attempt >= item.max_attempts:
                    self._transition(item, "failed", "attempts_exhausted")
                    item.retryable = False
                    item.error_class = "worker"
                    item.error_code = "worker_lease_exhausted"
                    item.last_error = "Worker lease expired and retry budget was exhausted"
                    item.completed_at = now
                    if item.reservation_id:
                        release(session, item.reservation_id)
                    trigger = session.get(CampaignMessage, item.trigger_message_id)
                    if trigger is not None:
                        trigger.status = "failed"
                        trigger.completed_at = now
                    if self.failure_recorder is not None:
                        self.failure_recorder(
                            session,
                            item,
                            RoomJobError(
                                "worker_lease_exhausted",
                                item.last_error,
                                False,
                                "worker",
                            ),
                        )
                else:
                    next_state = "waiting" if item.agent_result else "queued"
                    self._transition(item, next_state, "lease_expired")
                    item.available_at = now
                    item.last_error = "Recovered after worker lease expiry"
                    ROOM_TURN_JOB_RECOVERIES.labels(prior_status=prior).inc()
                item.lease_owner = None
                item.lease_expires_at = None
                item.heartbeat_at = None
                recovered += 1
            expire_abandoned(session, now=now)
            session.commit()
        return recovered

    def claim(self) -> str | None:
        now = now_utc()
        with self.factory() as session:
            candidate = session.execute(
                select(RoomTurnJob.id, RoomTurnJob.status)
                .where(
                    RoomTurnJob.status.in_(("queued", "waiting")),
                    RoomTurnJob.available_at <= now,
                    RoomTurnJob.lease_owner.is_(None),
                )
                .order_by(RoomTurnJob.available_at, RoomTurnJob.created_at)
                .limit(1)
            ).first()
            if candidate is None:
                return None
            job_id, prior_status = candidate
            claimed = session.execute(
                update(RoomTurnJob)
                .where(RoomTurnJob.id == job_id, RoomTurnJob.status == prior_status)
                .values(
                    status="running",
                    attempt=RoomTurnJob.attempt + 1,
                    started_at=func.coalesce(RoomTurnJob.started_at, now),
                    lease_owner=self.worker_id,
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            ROOM_TURN_JOB_TRANSITIONS.labels(
                from_status=prior_status,
                to_status="running",
                reason="claimed",
            ).inc()
            return str(job_id)

    def heartbeat(self, job_id: str) -> bool:
        now = now_utc()
        with self.factory() as session:
            item = session.scalar(
                select(RoomTurnJob)
                .where(RoomTurnJob.id == job_id, RoomTurnJob.lease_owner == self.worker_id)
                .with_for_update()
            )
            if item is None or item.status not in {"running", "waiting"}:
                return False
            item.heartbeat_at = now
            item.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            if item.reservation_id:
                try:
                    renew(
                        session,
                        item.reservation_id,
                        ttl_seconds=self.reservation_ttl_seconds,
                    )
                except QuotaReservationExpiredError:
                    return False
            session.commit()
            return not item.cancel_requested

    async def _heartbeat_loop(self, job_id: str) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            active = await self._database_retry(self.heartbeat, job_id)
            if not active:
                return

    async def _database_retry(self, function: Callable, *arguments):
        delay = 0.05
        for attempt in range(8):
            try:
                return await asyncio.to_thread(function, *arguments)
            except OperationalError:
                if attempt == 7:
                    raise
                await asyncio.sleep(delay)
                delay = min(0.5, delay * 2)
        raise RuntimeError("unreachable database retry state")

    def _record_failure(self, job_id: str, failure: RoomJobError) -> None:
        now = now_utc()
        with self.factory() as session:
            item = session.scalar(
                select(RoomTurnJob).where(RoomTurnJob.id == job_id).with_for_update()
            )
            if item is None or item.status in TERMINAL_ROOM_JOB_STATES:
                return
            item.error_code = failure.code
            item.error_class = failure.error_class
            item.last_error = failure.message[:1000]
            item.retryable = failure.retryable
            item.lease_owner = None
            item.lease_expires_at = None
            item.heartbeat_at = None
            terminal = False
            if item.cancel_requested:
                self._transition(item, "cancelled", "cancel_requested")
                item.completed_at = now
                terminal = True
                trigger = session.get(CampaignMessage, item.trigger_message_id)
                if trigger is not None:
                    trigger.status = "cancelled"
                    trigger.completed_at = now
                if item.reservation_id:
                    release(session, item.reservation_id)
            elif failure.retryable and item.attempt < item.max_attempts:
                next_state = "waiting" if item.agent_result else "queued"
                self._transition(item, next_state, "retryable_error")
                item.available_at = now + timedelta(
                    seconds=self.retry_seconds * max(1, item.attempt)
                )
            else:
                self._transition(item, "failed", "terminal_error")
                item.completed_at = now
                terminal = True
                trigger = session.get(CampaignMessage, item.trigger_message_id)
                if trigger is not None:
                    trigger.status = "failed"
                    trigger.completed_at = now
                if item.reservation_id:
                    release(session, item.reservation_id)
            if terminal and self.failure_recorder is not None:
                self.failure_recorder(session, item, failure)
            session.commit()

    async def _process(self, job_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(job_id))
        started = time.perf_counter()
        outcome = "success"
        execution_slot: asyncio.Semaphore | None = None
        room_id: str | None = None
        try:
            room_id = await self._database_retry(self._room_id, job_id)
            execution_slot = await self._acquire_execution_slot(room_id)
            await self.executor(job_id)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except RoomJobError as exc:
            outcome = "retry" if exc.retryable else "error"
            await self._database_retry(self._record_failure, job_id, exc)
        except Exception as exc:
            outcome = "retry"
            logger.exception("room turn job failed job_id=%s", job_id)
            await self._database_retry(
                self._record_failure,
                job_id,
                RoomJobError("room_turn_internal", str(exc), True, "internal"),
            )
        finally:
            if execution_slot is not None and room_id is not None:
                self._release_execution_slot(room_id, execution_slot)
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            ROOM_TURN_JOB_SECONDS.labels(phase="total", status=outcome).observe(
                time.perf_counter() - started
            )
            self._signal(job_id)

    def _room_id(self, job_id: str) -> str:
        with self.factory() as session:
            room_id = session.scalar(select(RoomTurnJob.room_id).where(RoomTurnJob.id == job_id))
        if room_id is None:
            raise RoomJobError("job_not_found", "Room turn job no longer exists", False, "state")
        return str(room_id)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._claim_lock:
                    await self._database_retry(self.recover_expired)
                    job_id = await self._database_retry(self.claim)
            except OperationalError:
                logger.exception("room turn queue database operation failed")
                await asyncio.sleep(self.poll_seconds)
                continue
            if job_id is not None:
                await self._process(job_id)
                continue
            await self._database_retry(self._observe_queue)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    def _observe_queue(self) -> None:
        with self.factory() as session:
            counts = dict(
                session.execute(
                    select(RoomTurnJob.status, func.count(RoomTurnJob.id)).group_by(
                        RoomTurnJob.status
                    )
                ).all()
            )
        for status in ("queued", "running", "waiting", "succeeded", "failed", "cancelled"):
            ROOM_TURN_JOB_QUEUE.labels(status=status).set(counts.get(status, 0))

    def _signal(self, job_id: str) -> None:
        for waiter in self._waiters.pop(job_id, set()):
            waiter.set()

    def _terminal(self, job_id: str) -> bool:
        with self.factory() as session:
            status = session.scalar(select(RoomTurnJob.status).where(RoomTurnJob.id == job_id))
        return status in TERMINAL_ROOM_JOB_STATES

    async def wait(self, job_id: str, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            if await self._database_retry(self._terminal, job_id):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            event = asyncio.Event()
            self._waiters.setdefault(job_id, set()).add(event)
            try:
                if await self._database_retry(self._terminal, job_id):
                    return True
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return False
            finally:
                waiters = self._waiters.get(job_id)
                if waiters is not None:
                    waiters.discard(event)
                    if not waiters:
                        self._waiters.pop(job_id, None)

    @asynccontextmanager
    async def settlement_lock(self, room_id: str) -> AsyncIterator[None]:
        """Serialize only the short Web publication transaction per room."""

        lock = self._settlement_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            yield
        if not lock.locked() and not getattr(lock, "_waiters", None):
            self._settlement_locks.pop(room_id, None)

    async def _acquire_execution_slot(self, room_id: str) -> asyncio.Semaphore:
        """Apply the configured per-room scheduler without holding a database lock.

        The job heartbeat starts before a claimed job waits for this slot, so a
        busy room cannot make a valid lease look abandoned. Independent rooms
        still consume the global worker pool concurrently.
        """

        slot = self._room_execution_slots.setdefault(
            room_id,
            asyncio.Semaphore(self.per_room_concurrency),
        )
        await slot.acquire()
        return slot

    def _release_execution_slot(self, room_id: str, slot: asyncio.Semaphore) -> None:
        slot.release()
        if (
            getattr(slot, "_value", 0) == self.per_room_concurrency
            and not getattr(slot, "_waiters", None)
        ):
            self._room_execution_slots.pop(room_id, None)

    @asynccontextmanager
    async def transaction_lock(self) -> AsyncIterator[None]:
        """Keep short Web write transactions from deadlocking SQLite.

        PostgreSQL still supplies the durable row-level authority. This local
        guard only serializes brief orchestration writes within one replica;
        no Agent, MCP, storage or other network await is performed under it.
        """

        async with self._transaction_lock:
            yield
