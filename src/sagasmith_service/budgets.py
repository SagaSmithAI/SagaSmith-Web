"""Fail-closed four-scope budget admission for paid provider requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.database import Base
from sagasmith_service.models import now_utc
from sagasmith_service.usage_accounting import provider_consumption_ledger

budget_limits = sa.Table(
    "budget_limits",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("scope_type", sa.String(16), nullable=False),
    sa.Column("scope_id", sa.String(160), nullable=False),
    sa.Column("metric", sa.String(50), nullable=False),
    sa.Column("quantity", sa.Numeric(24, 6), nullable=False),
    sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default="active"),
    sa.Column("source", sa.String(50), nullable=False, server_default="admin"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "scope_type",
        "scope_id",
        "metric",
        "period_start",
        name="uq_budget_limit_period",
    ),
    sa.Index("ix_budget_limit_lookup", "scope_type", "scope_id", "metric", "period_start"),
)

budget_claims = sa.Table(
    "budget_claims",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("reservation_key", sa.String(200), nullable=False),
    sa.Column("scope_type", sa.String(16), nullable=False),
    sa.Column("scope_id", sa.String(160), nullable=False),
    sa.Column("metric", sa.String(50), nullable=False),
    sa.Column("quantity", sa.Numeric(24, 6), nullable=False),
    sa.Column("settled_quantity", sa.Numeric(24, 6), nullable=False, server_default="0"),
    sa.Column("unknown_reason", sa.String(500), nullable=True),
    sa.Column("status", sa.String(24), nullable=False, server_default="reserved"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("reservation_key", "scope_type", name="uq_budget_claim_reservation_scope"),
    sa.Index("ix_budget_claim_lookup", "scope_type", "scope_id", "metric", "status"),
)


class BudgetUnavailableError(ValueError):
    """Raised when a required scope has no configured budget."""


class BudgetExceededError(ValueError):
    """Raised before a paid request when a scope cannot admit it."""


class BudgetClaimClosedError(ValueError):
    """Raised when a retry tries to reuse a settled or released call claim."""


class BudgetClaimPendingError(ValueError):
    """Raised when a prior provider attempt has an unknown outcome."""


@dataclass(frozen=True)
class BudgetScope:
    scope_type: str
    scope_id: str
    limit: Decimal
    consumed: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return max(Decimal("0"), self.limit - self.consumed - self.reserved)


def _scopes(*, user_id: str, campaign_id: str | None, task_id: str | None) -> list[tuple[str, str]]:
    if not campaign_id or not task_id:
        raise BudgetUnavailableError("campaign and task budget identities are required")
    return [
        ("site", "default"),
        ("user", user_id),
        ("campaign", campaign_id),
        ("task", task_id),
    ]


def _scope_status(
    session: Session,
    *,
    scope_type: str,
    scope_id: str,
    metric: str,
    quantity: Decimal,
    now: datetime,
) -> BudgetScope:
    rows = (
        session.execute(
            select(budget_limits).where(
                budget_limits.c.scope_type == scope_type,
                budget_limits.c.scope_id == scope_id,
                budget_limits.c.metric == metric,
                budget_limits.c.status == "active",
                budget_limits.c.period_start <= now,
                budget_limits.c.period_end > now,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise BudgetUnavailableError(
            f"{scope_type} budget is not configured for {metric}: {scope_id}"
        )
    limit = sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))
    period_filter = sa.or_(
        *(
            sa.and_(
                provider_consumption_ledger.c.occurred_at >= row["period_start"],
                provider_consumption_ledger.c.occurred_at < row["period_end"],
            )
            for row in rows
        )
    )
    consumed = Decimal(
        session.scalar(
            select(sa.func.coalesce(sa.func.sum(provider_consumption_ledger.c.quantity), 0)).where(
                provider_consumption_ledger.c.metric == metric,
                period_filter,
                (
                    provider_consumption_ledger.c.user_id == scope_id
                    if scope_type == "user"
                    else provider_consumption_ledger.c.campaign_id == scope_id
                    if scope_type == "campaign"
                    else provider_consumption_ledger.c.task_id == scope_id
                    if scope_type == "task"
                    else provider_consumption_ledger.c.id.is_not(None)
                ),
            )
        )
    )
    reserved = Decimal(
        session.scalar(
            select(sa.func.coalesce(sa.func.sum(budget_claims.c.quantity), 0)).where(
                budget_claims.c.scope_type == scope_type,
                budget_claims.c.scope_id == scope_id,
                budget_claims.c.metric == metric,
                budget_claims.c.status.in_(("reserved", "pending_reconciliation")),
            )
        )
    )
    return BudgetScope(scope_type, scope_id, limit, consumed, reserved)


def admit(
    session: Session,
    *,
    user_id: str,
    campaign_id: str | None,
    task_id: str | None,
    metric: str,
    quantity: Decimal,
    reservation_key: str,
    now: datetime | None = None,
) -> tuple[BudgetScope, ...]:
    """Atomically claim all four budgets before a provider request.

    Missing configuration is an error by design.  Callers must fail closed
    before contacting a paid provider and release claims if quota reservation
    subsequently fails.
    """

    if quantity <= 0:
        raise ValueError("budget quantity must be positive")
    scopes = _scopes(user_id=user_id, campaign_id=campaign_id, task_id=task_id)
    current = now or now_utc()
    existing = (
        session.execute(
            select(budget_claims).where(budget_claims.c.reservation_key == reservation_key)
        )
        .mappings()
        .all()
    )
    if existing:
        if any(Decimal(row["quantity"]) != quantity for row in existing):
            raise ValueError("budget reservation key payload mismatch")
        expected_scopes = set(scopes)
        actual_scopes = {(str(row["scope_type"]), str(row["scope_id"])) for row in existing}
        if actual_scopes != expected_scopes or any(row["metric"] != metric for row in existing):
            raise ValueError("budget reservation key scope or metric mismatch")
        statuses = {str(row["status"]) for row in existing}
        if statuses & {"settled", "reconciled", "overrun", "released"}:
            raise BudgetClaimClosedError("budget reservation has already been closed")
        if "pending_reconciliation" in statuses:
            raise BudgetClaimPendingError("budget reservation has an unknown provider outcome")
        return tuple(
            BudgetScope(
                str(row["scope_type"]),
                str(row["scope_id"]),
                Decimal("0"),
                Decimal("0"),
                Decimal(row["quantity"]),
            )
            for row in existing
        )
    # Lock in a deterministic order before reading balances.  PostgreSQL uses
    # row locks; the no-op update also obtains SQLite's writer lock so two
    # admission transactions cannot both pass the same site limit.
    session.execute(
        budget_limits.update()
        .where(
            budget_limits.c.scope_type == "site",
            budget_limits.c.scope_id == "default",
            budget_limits.c.metric == metric,
            budget_limits.c.status == "active",
            budget_limits.c.period_start <= current,
            budget_limits.c.period_end > current,
        )
        .values(quantity=budget_limits.c.quantity)
    )
    statuses = tuple(
        _scope_status(
            session,
            scope_type=scope_type,
            scope_id=scope_id,
            metric=metric,
            quantity=quantity,
            now=current,
        )
        for scope_type, scope_id in scopes
    )
    for status in statuses:
        if status.available < quantity:
            raise BudgetExceededError(
                f"{status.scope_type} budget exceeded: requested {quantity}, "
                f"available {status.available}"
            )
    import uuid

    for scope_type, scope_id in scopes:
        session.execute(
            budget_claims.insert().values(
                id=str(uuid.uuid4()),
                reservation_key=reservation_key,
                scope_type=scope_type,
                scope_id=scope_id,
                metric=metric,
                quantity=quantity,
                status="reserved",
                created_at=current,
            )
        )
    session.flush()
    return statuses


def release(session: Session, reservation_key: str) -> None:
    session.execute(
        budget_claims.update()
        .where(
            budget_claims.c.reservation_key == reservation_key,
            budget_claims.c.status == "reserved",
        )
        .values(status="released")
    )


def settle(session: Session, reservation_key: str, quantity: Decimal) -> None:
    """Close a per-call claim after provider accounting is durable.

    ``quantity`` is the uncapped provider amount.  A provider overrun is
    recorded as ``overrun`` and is therefore visible to reconciliation while
    the claim itself no longer blocks later requests.
    """

    if quantity < 0:
        raise ValueError("budget settlement quantity cannot be negative")
    session.execute(
        budget_claims.update()
        .where(
            budget_claims.c.reservation_key == reservation_key,
            budget_claims.c.status == "reserved",
        )
        .values(
            status=sa.case(
                (quantity > budget_claims.c.quantity, "overrun"),
                else_="settled",
            ),
            settled_quantity=quantity,
        )
    )


def mark_unknown(session: Session, reservation_key: str, reason: str) -> None:
    """Keep a claim blocking its task until an operator/provider reconciles it."""

    session.execute(
        budget_claims.update()
        .where(
            budget_claims.c.reservation_key == reservation_key,
            budget_claims.c.status == "reserved",
        )
        .values(status="pending_reconciliation", unknown_reason=reason[:500])
    )


def reconcile(session: Session, reservation_key: str, quantity: Decimal) -> None:
    """Close a previously unknown outcome after provider reconciliation."""

    if quantity < 0:
        raise ValueError("budget reconciliation quantity cannot be negative")
    session.execute(
        budget_claims.update()
        .where(
            budget_claims.c.reservation_key == reservation_key,
            budget_claims.c.status == "pending_reconciliation",
        )
        .values(
            status=sa.case(
                (quantity > budget_claims.c.quantity, "overrun"),
                else_="reconciled",
            ),
            settled_quantity=quantity,
        )
    )


authorize = admit
