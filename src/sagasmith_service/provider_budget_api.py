"""Internal per-provider-call admission and durable, versioned cost settlement."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

import jwt
import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from sagasmith_service import budgets
from sagasmith_service.api.dependencies import DbSession
from sagasmith_service.config import ProviderPrice, Settings
from sagasmith_service.database import Base
from sagasmith_service.models import User, now_utc
from sagasmith_service.usage_accounting import record_provider_consumption

METRIC = "provider_cost_usd"
AUDIENCE = "sagasmith-provider-budget"
router = APIRouter(prefix="/internal/provider-budget", include_in_schema=False)
provider_calls = sa.Table(
    "provider_calls", Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("task_id", sa.String(160), nullable=False, index=True),
    sa.Column("provider", sa.String(120), nullable=False),
    sa.Column("model", sa.String(120), nullable=False),
    sa.Column("price", sa.JSON, nullable=False),
    sa.Column("reserved_usd", sa.Numeric(24, 6), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("settlement_hash", sa.String(64)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class AuthorizeCall(BaseModel):
    call_id: uuid.UUID
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    max_output_tokens: int = Field(gt=0)
    request_bytes: int = Field(ge=0)


class SettleCall(BaseModel):
    reservation_id: uuid.UUID
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str = Field(max_length=80)
    request_id: str | None = Field(default=None, max_length=100)


def callback_for(settings: Settings, context: dict[str, Any]) -> dict[str, str] | None:
    if not settings.provider_budget_enabled:
        return None
    authority = context.get("authority_context") or {}
    principal = str(authority.get("requester_principal") or "")
    user_id = context.get("budget_user_id") or (
        principal.removeprefix("user:") if principal.startswith("user:") else ""
    )
    campaign_id = authority.get("campaign_id") or context.get("campaign_id")
    task_id = authority.get("room_turn_id") or context.get("run_id")
    if not user_id or not campaign_id or not task_id:
        raise ValueError("provider budget requires trusted user, campaign and task identities")
    now = now_utc()
    token = jwt.encode({
        "aud": AUDIENCE, "sub": str(user_id), "campaign": str(campaign_id),
        "task": str(task_id), "iat": now, "exp": now + timedelta(hours=2),
    }, settings.auth_context_secret.get_secret_value(), algorithm="HS256")
    base = settings.service_internal_url.rstrip("/") + "/internal/provider-budget"
    return {"authorize_url": base + "/authorize", "settle_url": base + "/settle", "token": token}


def _claims(request: Request, authorization: str) -> dict:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(401, "provider budget authentication required")
    try:
        claims = jwt.decode(
            token, request.app.state.settings.auth_context_secret.get_secret_value(),
            algorithms=["HS256"], audience=AUDIENCE,
            options={"require": ["sub", "campaign", "task", "iat", "exp", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "invalid provider budget credential") from exc
    return claims


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


@router.post("/authorize")
def authorize_call(
    payload: AuthorizeCall, request: Request, session: DbSession,
    authorization: str = Header(default=""),
) -> dict:
    claims = _claims(request, authorization)
    settings = request.app.state.settings
    if not settings.provider_budget_enabled:
        raise HTTPException(503, "provider budgets are disabled")
    price = settings.provider_prices.get(payload.model)
    if not price or price.provider != payload.provider:
        raise HTTPException(403, "provider/model has no reviewed price")
    if price.valid_until.tzinfo is None or price.valid_until.astimezone(UTC) <= now_utc():
        raise HTTPException(403, "provider price review has expired")
    if (payload.max_output_tokens > price.max_output_tokens
            or payload.request_bytes > price.max_request_bytes):
        raise HTTPException(413, "model request exceeds its reviewed bounds")
    # Serialize authorization and settlement with the same site row before any reads.
    session.execute(budgets.budget_limits.update().where(
        budgets.budget_limits.c.scope_type == "site",
        budgets.budget_limits.c.scope_id == "default",
        budgets.budget_limits.c.metric == METRIC,
    ).values(quantity=budgets.budget_limits.c.quantity))
    user = session.get(User, claims["sub"])
    if not user or user.status != "active":
        raise HTTPException(403, "budget account is inactive")
    if session.scalar(sa.select(provider_calls.c.id).where(
        provider_calls.c.id == str(payload.call_id)
    )):
        raise HTTPException(
            409, "provider attempt already authorized; reconcile instead of replaying"
        )
    if session.scalar(sa.select(provider_calls.c.id).where(
        provider_calls.c.task_id == claims["task"],
        provider_calls.c.status.in_(["reserved", "unknown", "overrun"]),
    )):
        raise HTTPException(409, "previous provider attempt requires reconciliation")
    now = now_utc()
    # A task receives one finite lifetime cap; never replenish on replay/month rollover.
    if not session.scalar(sa.select(budgets.budget_limits.c.id).where(
        budgets.budget_limits.c.scope_type == "task",
        budgets.budget_limits.c.scope_id == claims["task"],
        budgets.budget_limits.c.metric == METRIC,
    )):
        session.execute(budgets.budget_limits.insert().values(
            id=str(uuid.uuid4()), scope_type="task", scope_id=claims["task"], metric=METRIC,
            quantity=settings.task_budget_usd, period_start=now,
            period_end=now + timedelta(days=36500), status="active", source="task-policy",
            created_at=now,
        ))
    worst = _money((
        price.max_input_tokens * max(price.input_usd_per_million, price.cached_usd_per_million)
        + payload.max_output_tokens * price.output_usd_per_million
    ) / 1_000_000)
    try:
        budgets.admit(
            session, user_id=claims["sub"], campaign_id=claims["campaign"],
            task_id=claims["task"], metric=METRIC, quantity=worst,
            reservation_key=str(payload.call_id),
        )
    except ValueError as exc:
        raise HTTPException(402, "provider budget is unavailable or exhausted") from exc
    session.execute(provider_calls.insert().values(
        id=str(payload.call_id), user_id=claims["sub"], campaign_id=claims["campaign"],
        task_id=claims["task"], provider=payload.provider, model=payload.model,
        price=price.model_dump(mode="json"), reserved_usd=worst, status="reserved", created_at=now,
    ))
    session.commit()
    return {"reservation_id": str(payload.call_id)}


@router.post("/settle")
def settle_call(
    payload: SettleCall, request: Request, session: DbSession,
    authorization: str = Header(default=""),
) -> dict:
    claims = _claims(request, authorization)
    session.execute(budgets.budget_limits.update().where(
        budgets.budget_limits.c.scope_type == "site",
        budgets.budget_limits.c.scope_id == "default",
        budgets.budget_limits.c.metric == METRIC,
    ).values(quantity=budgets.budget_limits.c.quantity))
    call = session.execute(sa.select(provider_calls).where(
        provider_calls.c.id == str(payload.reservation_id),
        provider_calls.c.user_id == claims["sub"],
        provider_calls.c.campaign_id == claims["campaign"],
        provider_calls.c.task_id == claims["task"],
    )).mappings().first()
    if not call:
        raise HTTPException(404, "provider reservation not found")
    return settle_record(session, call, payload)


def settle_record(session, call, payload: SettleCall) -> dict:
    """Settle a previously authorized call under the caller's site lock."""
    digest = hashlib.sha256(json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    if call["settlement_hash"]:
        if call["settlement_hash"] != digest:
            raise HTTPException(409, "provider settlement changed on replay")
        return {"status": call["status"]}
    usage = payload.usage
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    output = usage.get("completion_tokens", usage.get("output_tokens"))
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = usage.get("cached_tokens", usage.get("cache_read_input_tokens", usage.get(
        "prompt_cache_hit_tokens", details.get("cached_tokens", 0)
    )))
    counts = (prompt, output, cached)
    known = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts)
    known = known and cached <= prompt
    # This beta price schema does not price Anthropic-style cache creation separately.
    known = known and not usage.get("cache_creation_input_tokens")
    if not known:
        budgets.mark_unknown(session, call["id"], "provider returned incomplete usage")
        state = "unknown"
    else:
        price = ProviderPrice.model_validate(call["price"])
        cost = _money((
            (prompt - cached) * price.input_usd_per_million
            + cached * price.cached_usd_per_million + output * price.output_usd_per_million
        ) / 1_000_000)
        record_provider_consumption(
            session, user_id=call["user_id"], campaign_id=call["campaign_id"],
            task_id=call["task_id"], reservation_id=call["id"], metric=METRIC, quantity=cost,
            unit="USD", idempotency_key="provider-call:" + call["id"],
            provider=call["provider"], model=call["model"], request_id=payload.request_id,
            pricing_version=price.version,
            pricing_inputs={"tokens": prompt, "usd_per_million": str(price.input_usd_per_million)},
            pricing_cache={"tokens": cached, "usd_per_million": str(price.cached_usd_per_million)},
            pricing_output={"tokens": output, "usd_per_million": str(price.output_usd_per_million)},
            cost_amount=cost, cost_currency="USD", details={"finish_reason": payload.finish_reason},
        )
        budgets.settle(session, call["id"], cost)
        state = "overrun" if cost > call["reserved_usd"] else "settled"
    session.execute(provider_calls.update().where(provider_calls.c.id == call["id"]).values(
        status=state, settlement_hash=digest,
    ))
    session.commit()
    return {"status": state}
