"""Durable provider consumption accounting separate from entitlement quota."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.database import Base
from sagasmith_service.models import now_utc

provider_consumption_ledger = sa.Table(
    "provider_consumption_ledger",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "user_id",
        sa.String(36),
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("campaign_id", sa.String(64), nullable=True, index=True),
    sa.Column("task_id", sa.String(160), nullable=True, index=True),
    sa.Column("reservation_id", sa.String(36), nullable=True),
    sa.Column("metric", sa.String(50), nullable=False),
    sa.Column("quantity", sa.Numeric(24, 6), nullable=False),
    sa.Column("unit", sa.String(32), nullable=False),
    sa.Column("provider", sa.String(80), nullable=True),
    sa.Column("model", sa.String(120), nullable=True),
    sa.Column("request_id", sa.String(100), nullable=True, index=True),
    sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
    sa.Column("pricing_version", sa.String(80), nullable=True),
    sa.Column("pricing_inputs", sa.JSON(), nullable=True),
    sa.Column("pricing_cache", sa.JSON(), nullable=True),
    sa.Column("pricing_output", sa.JSON(), nullable=True),
    sa.Column("cost_amount", sa.Numeric(24, 12), nullable=True),
    sa.Column("cost_currency", sa.String(12), nullable=True),
    sa.Column("cost_status", sa.String(24), nullable=False, server_default="unknown"),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
    sa.Index(
        "ix_provider_consumption_period",
        "user_id",
        "metric",
        "occurred_at",
    ),
)


@dataclass(frozen=True)
class ProviderConsumption:
    id: str
    user_id: str
    quantity: Decimal
    unit: str
    idempotency_key: str
    request_id: str | None
    provider: str | None
    model: str | None
    cost_status: str


def _row_to_consumption(row: Any) -> ProviderConsumption:
    def value(name: str) -> Any:
        if isinstance(row, Mapping):
            return row[name]
        return getattr(row, name)

    return ProviderConsumption(
        id=str(value("id")),
        user_id=str(value("user_id")),
        quantity=Decimal(value("quantity")),
        unit=str(value("unit")),
        idempotency_key=str(value("idempotency_key")),
        request_id=value("request_id"),
        provider=value("provider"),
        model=value("model"),
        cost_status=str(value("cost_status")),
    )


def record_provider_consumption(
    session: Session,
    *,
    user_id: str,
    campaign_id: str | None,
    task_id: str | None,
    reservation_id: str | None,
    metric: str,
    quantity: Decimal,
    unit: str,
    idempotency_key: str,
    provider: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    pricing_version: str | None = None,
    pricing_inputs: dict[str, Any] | None = None,
    pricing_cache: dict[str, Any] | None = None,
    pricing_output: dict[str, Any] | None = None,
    cost_amount: Decimal | None = None,
    cost_currency: str | None = None,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ProviderConsumption:
    """Write one uncapped provider record, safely replayable after restart.

    Cost stays explicitly ``unknown`` until a versioned pricing input and
    output are available.  Entitlement settlement is deliberately handled by
    :func:`sagasmith_service.quota.settle` and is allowed to be lower than
    this provider record when a provider overruns a reservation.
    """

    if quantity < 0:
        raise ValueError("provider consumption quantity cannot be negative")
    existing = (
        session.execute(
            select(provider_consumption_ledger).where(
                provider_consumption_ledger.c.idempotency_key == idempotency_key
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        if (
            Decimal(existing["quantity"]) != quantity
            or existing["user_id"] != user_id
            or existing["metric"] != metric
        ):
            raise ValueError("provider consumption idempotency key payload mismatch")
        return _row_to_consumption(existing)
    if cost_amount is not None and cost_amount < 0:
        raise ValueError("provider cost cannot be negative")
    if cost_amount is None or cost_currency is None or pricing_version is None:
        cost_status = "unknown"
        cost_amount = None
        cost_currency = None
    else:
        cost_status = "known"
    import uuid

    values = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "campaign_id": campaign_id,
        "task_id": task_id,
        "reservation_id": reservation_id,
        "metric": metric,
        "quantity": quantity,
        "unit": unit,
        "provider": provider,
        "model": model,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "pricing_version": pricing_version,
        "pricing_inputs": pricing_inputs,
        "pricing_cache": pricing_cache,
        "pricing_output": pricing_output,
        "cost_amount": cost_amount,
        "cost_currency": cost_currency,
        "cost_status": cost_status,
        "occurred_at": occurred_at or now_utc(),
        "details": details or {},
    }
    try:
        with session.begin_nested():
            session.execute(provider_consumption_ledger.insert().values(**values))
            session.flush()
    except sa.exc.IntegrityError:
        existing = (
            session.execute(
                select(provider_consumption_ledger).where(
                    provider_consumption_ledger.c.idempotency_key == idempotency_key
                )
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise
        if Decimal(existing["quantity"]) != quantity or existing["user_id"] != user_id:
            raise ValueError("provider consumption idempotency key payload mismatch")
        return _row_to_consumption(existing)
    return _row_to_consumption(values)
