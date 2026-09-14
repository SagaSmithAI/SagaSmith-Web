from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from sagasmith_service.usage_accounting import provider_consumption_ledger  # noqa: F401


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


def _grant_allocations(details: object) -> dict[str, Decimal]:
    """Read settlement allocations without trusting malformed historical JSON."""

    if not isinstance(details, dict):
        return {}
    raw = details.get("grant_allocations")
    if not isinstance(raw, list):
        return {}
    allocations: dict[str, Decimal] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("grant_id"):
            continue
        try:
            quantity = Decimal(str(item.get("quantity", "0")))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if quantity > 0:
            allocations[str(item["grant_id"])] = (
                allocations.get(str(item["grant_id"]), Decimal("0")) + quantity
            )
    return allocations


def _allocate_to_grants(
    session: Session,
    *,
    user_id: str,
    metric: str,
    quantity: Decimal,
    occurred_at,
) -> list[dict[str, str]]:
    """Allocate entitlement usage to grants active at settlement time.

    The existing ``usage_ledger`` table predates grant allocation.  New rows
    carry explicit allocations in ``details``; old rows remain supported by
    the period-aware balance calculation below.
    """

    if quantity <= 0:
        return []
    grants = session.scalars(
        select(QuotaGrant)
        .where(
            QuotaGrant.user_id == user_id,
            QuotaGrant.metric == metric,
            QuotaGrant.period_start <= occurred_at,
            QuotaGrant.period_end > occurred_at,
        )
        .order_by(QuotaGrant.period_end, QuotaGrant.period_start, QuotaGrant.id)
        .with_for_update()
    ).all()
    if not grants:
        return []
    consumed: dict[str, Decimal] = {grant.id: Decimal("0") for grant in grants}
    historical = session.scalars(
        select(UsageLedger).where(
            UsageLedger.user_id == user_id,
            UsageLedger.metric == metric,
            UsageLedger.occurred_at <= occurred_at,
        )
    ).all()
    for usage in historical:
        for grant_id, used in _grant_allocations(usage.details).items():
            if grant_id in consumed:
                consumed[grant_id] += used
    remaining = quantity
    allocations: list[dict[str, str]] = []
    for grant in grants:
        capacity = max(Decimal("0"), Decimal(grant.quantity) - consumed[grant.id])
        amount = min(remaining, capacity)
        if amount > 0:
            allocations.append({"grant_id": grant.id, "quantity": str(amount)})
            remaining -= amount
        if remaining <= 0:
            break
    return allocations


def _period_contains(grant: QuotaGrant, occurred_at: datetime) -> bool:
    """Compare SQLite's naive UTC datetimes with application UTC values."""

    timestamp = (
        occurred_at
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC).replace(tzinfo=None)
    )
    period_start = grant.period_start
    period_end = grant.period_end
    if period_start.tzinfo is not None:
        period_start = period_start.astimezone(UTC).replace(tzinfo=None)
    if period_end.tzinfo is not None:
        period_end = period_end.astimezone(UTC).replace(tzinfo=None)
    return period_start <= timestamp < period_end


def balance(session: Session, user_id: str, metric: str) -> QuotaBalance:
    now = now_utc()
    expire_abandoned(session, now=now)
    active_grants = session.scalars(
        select(QuotaGrant.id)
        .where(
            QuotaGrant.user_id == user_id,
            QuotaGrant.metric == metric,
            QuotaGrant.period_start <= now,
            QuotaGrant.period_end > now,
        )
        .with_for_update()
    ).all()
    active_grant_ids = set(active_grants)
    all_grants = session.scalars(
        select(QuotaGrant)
        .where(
            QuotaGrant.user_id == user_id,
            QuotaGrant.metric == metric,
        )
        .order_by(QuotaGrant.period_end, QuotaGrant.period_start, QuotaGrant.id)
    ).all()
    active_grants_by_id = {grant.id: grant for grant in all_grants if grant.id in active_grant_ids}
    granted = sum(
        (Decimal(grant.quantity) for grant in active_grants_by_id.values()), Decimal("0")
    )
    # New ledger rows carry explicit grant allocations.  Restrict those
    # allocations to currently active grants so a completed monthly grant
    # cannot consume a later period's balance.  Legacy rows without
    # allocations are assigned to the earliest-ending grant that covered the
    # usage; this preserves the old period semantics when grants overlap.
    # Preserve the existing API distinction: an empty active period reports a
    # six-place zero, while a user with no active grant reports an unscaled zero.
    used = Decimal("0.000000") if active_grant_ids else Decimal("0")
    usage_rows = session.scalars(
        select(UsageLedger)
        .where(
            UsageLedger.user_id == user_id,
            UsageLedger.metric == metric,
            UsageLedger.occurred_at <= now,
        )
        .order_by(UsageLedger.occurred_at, UsageLedger.id)
    ).all()
    for usage in usage_rows:
        allocations = _grant_allocations(usage.details)
        if allocations:
            used += sum(
                (
                    quantity
                    for grant_id, quantity in allocations.items()
                    if grant_id in active_grant_ids
                ),
                Decimal("0"),
            )
            continue
        matching_grants = [
            grant
            for grant in all_grants
            if _period_contains(grant, usage.occurred_at)
        ]
        if matching_grants and matching_grants[0].id in active_grant_ids:
            used += Decimal(usage.quantity)
    if used:
        used = used.quantize(Decimal("0.000001"))
    reserved = session.scalar(
        select(func.coalesce(func.sum(QuotaReservation.reserved_quantity), 0)).where(
            QuotaReservation.user_id == user_id,
            QuotaReservation.metric == metric,
            QuotaReservation.status == "reserved",
        )
    )
    return QuotaBalance(
        metric,
        granted,
        used,
        Decimal(reserved),
    )


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
    details: dict | None = None,
) -> UsageLedger:
    existing = session.scalar(
        select(UsageLedger).where(UsageLedger.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if (
            existing.reservation_id != reservation_id
            or existing.quantity != quantity
            or existing.unit != unit
        ):
            raise ValueError("usage idempotency key payload mismatch")
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
    occurred_at = now_utc()
    allocation_details = dict(details or {})
    allocations = _allocate_to_grants(
        session,
        user_id=reservation.user_id,
        metric=reservation.metric,
        quantity=quantity,
        occurred_at=occurred_at,
    )
    if allocations:
        allocation_details["grant_allocations"] = allocations
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
        occurred_at=occurred_at,
        details=allocation_details,
    )
    reservation.settled_quantity = quantity
    reservation.status = "settled"
    reservation.settled_at = occurred_at
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
