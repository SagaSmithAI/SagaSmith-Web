from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sagasmith_service.models import (
    ModuleRun,
    QuotaGrant,
    QuotaReservation,
    RoomTurnJob,
    UsageLedger,
    now_utc,
)


class QuotaExceededError(ValueError):
    pass


class QuotaReservationExpiredError(ValueError):
    pass


@dataclass(frozen=True)
class QuotaBalance:
    metric: str
    granted: Decimal
    used: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return max(Decimal("0"), self.granted - self.used - self.reserved)


def balance(session: Session, user_id: str, metric: str) -> QuotaBalance:
    now = now_utc()
    expire_abandoned(session, now=now)
    session.execute(
        select(QuotaGrant.id)
        .where(
            QuotaGrant.user_id == user_id,
            QuotaGrant.metric == metric,
            QuotaGrant.period_start <= now,
            QuotaGrant.period_end > now,
        )
        .with_for_update()
    ).all()
    granted = session.scalar(
        select(func.coalesce(func.sum(QuotaGrant.quantity), 0)).where(
            QuotaGrant.user_id == user_id,
            QuotaGrant.metric == metric,
            QuotaGrant.period_start <= now,
            QuotaGrant.period_end > now,
        )
    )
    used = session.scalar(
        select(func.coalesce(func.sum(UsageLedger.quantity), 0)).where(
            UsageLedger.user_id == user_id,
            UsageLedger.metric == metric,
            UsageLedger.occurred_at <= now,
        )
    )
    reserved = session.scalar(
        select(func.coalesce(func.sum(QuotaReservation.reserved_quantity), 0)).where(
            QuotaReservation.user_id == user_id,
            QuotaReservation.metric == metric,
            QuotaReservation.status == "reserved",
        )
    )
    return QuotaBalance(metric, Decimal(granted), Decimal(used), Decimal(reserved))


def reserve(
    session: Session,
    *,
    user_id: str,
    campaign_id: str | None,
    metric: str,
    quantity: Decimal,
    idempotency_key: str,
    ttl_seconds: int = 300,
) -> QuotaReservation:
    existing = session.scalar(
        select(QuotaReservation).where(
            QuotaReservation.user_id == user_id,
            QuotaReservation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.campaign_id != campaign_id
            or existing.metric != metric
            or existing.reserved_quantity != quantity
        ):
            raise ValueError("reservation idempotency key payload mismatch")
        return existing
    current = balance(session, user_id, metric)
    if quantity <= 0:
        raise ValueError("reservation quantity must be positive")
    if current.available < quantity:
        raise QuotaExceededError(
            f"insufficient {metric}: requested {quantity}, available {current.available}"
        )
    item = QuotaReservation(
        user_id=user_id,
        campaign_id=campaign_id,
        metric=metric,
        reserved_quantity=quantity,
        idempotency_key=idempotency_key,
        expires_at=now_utc() + timedelta(seconds=ttl_seconds),
    )
    session.add(item)
    session.flush()
    return item


def renew(
    session: Session,
    reservation_id: str,
    *,
    ttl_seconds: int,
) -> QuotaReservation:
    """Extend one active reservation while holding its row lock."""

    if ttl_seconds <= 0:
        raise ValueError("reservation renewal TTL must be positive")
    reservation = session.scalar(
        select(QuotaReservation).where(QuotaReservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise ValueError("reservation not found")
    if reservation.status != "reserved":
        raise QuotaReservationExpiredError(
            f"reservation is no longer renewable: {reservation.status}"
        )
    reservation.expires_at = now_utc() + timedelta(seconds=ttl_seconds)
    return reservation


def expire_abandoned(session: Session, *, now=None) -> int:
    """Release only expired reservations that no durable worker still owns.

    Merely crossing ``expires_at`` does not remove a reservation from balance:
    an active durable job may be recovering after a process crash.  This reaper
    makes expiry explicit and therefore closes the old over-allocation window.
    """

    now = now or now_utc()
    rows = session.scalars(
        select(QuotaReservation)
        .where(
            QuotaReservation.status == "reserved",
            QuotaReservation.expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    expired = 0
    active_room_statuses = ("queued", "running", "waiting")
    active_module_statuses = ("queued", "running")
    for reservation in rows:
        room_owner = session.scalar(
            select(RoomTurnJob.id)
            .where(
                RoomTurnJob.reservation_id == reservation.id,
                RoomTurnJob.status.in_(active_room_statuses),
            )
            .limit(1)
        )
        module_owner = session.scalar(
            select(ModuleRun.id)
            .where(
                ModuleRun.reservation_id == reservation.id,
                ModuleRun.status.in_(active_module_statuses),
            )
            .limit(1)
        )
        if room_owner is not None or module_owner is not None:
            continue
        reservation.status = "expired"
        reservation.settled_at = now
        expired += 1
    return expired


def settle(
    session: Session,
    *,
    reservation_id: str,
    quantity: Decimal,
    idempotency_key: str,
    unit: str,
    provider: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
) -> UsageLedger:
    existing = session.scalar(
        select(UsageLedger).where(UsageLedger.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    reservation = session.scalar(
        select(QuotaReservation).where(QuotaReservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise ValueError("reservation not found")
    if reservation.status == "settled":
        settled = session.scalar(
            select(UsageLedger).where(UsageLedger.reservation_id == reservation.id)
        )
        if settled is not None and settled.quantity == quantity:
            return settled
        raise ValueError("reservation was already settled with a different quantity")
    if reservation.status != "reserved":
        raise ValueError(f"reservation cannot be settled: {reservation.status}")
    if quantity < 0 or quantity > reservation.reserved_quantity:
        raise ValueError("settled quantity must fit within the reservation")
    item = UsageLedger(
        user_id=reservation.user_id,
        campaign_id=reservation.campaign_id,
        reservation_id=reservation.id,
        metric=reservation.metric,
        quantity=quantity,
        unit=unit,
        provider=provider,
        model=model,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )
    reservation.settled_quantity = quantity
    reservation.status = "settled"
    reservation.settled_at = now_utc()
    session.add(item)
    session.flush()
    return item


def release(session: Session, reservation_id: str) -> None:
    reservation = session.scalar(
        select(QuotaReservation).where(QuotaReservation.id == reservation_id).with_for_update()
    )
    if reservation is not None and reservation.status == "reserved":
        reservation.status = "released"
        reservation.settled_at = now_utc()
