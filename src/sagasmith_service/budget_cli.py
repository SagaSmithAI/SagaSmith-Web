"""Offline budget management and documented provider-outcome reconciliation."""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from sagasmith_service import budgets
from sagasmith_service.database import make_engine, make_session_factory
from sagasmith_service.models import AuditEvent, now_utc
from sagasmith_service.provider_budget_api import METRIC, SettleCall, provider_calls, settle_record


def set_limit(session, *, scope: str, identity: str, usd: Decimal, days: int, reason: str):
    if not usd.is_finite() or usd < 0 or days < 1:
        raise ValueError("budget must be finite/nonnegative and days positive")
    now = now_utc()
    active = session.execute(select(budgets.budget_limits).where(
        budgets.budget_limits.c.scope_type == scope,
        budgets.budget_limits.c.scope_id == identity,
        budgets.budget_limits.c.metric == METRIC,
        budgets.budget_limits.c.status == "active",
        budgets.budget_limits.c.period_start <= now,
        budgets.budget_limits.c.period_end > now,
    )).mappings().all()
    if len(active) > 1:
        raise ValueError("overlapping budgets require operator review")
    if active:
        # Changing a cap never resets already-recorded usage or extends the period.
        session.execute(budgets.budget_limits.update().where(
            budgets.budget_limits.c.id == active[0]["id"]
        ).values(quantity=usd))
        budget_id = active[0]["id"]
    else:
        budget_id = str(uuid.uuid4())
        session.execute(budgets.budget_limits.insert().values(
            id=budget_id, scope_type=scope, scope_id=identity, metric=METRIC,
            quantity=usd, period_start=now, period_end=now + timedelta(days=days),
            status="active", source="offline-admin", created_at=now,
        ))
    session.add(AuditEvent(
        action="budget.set_limit", subject_type="budget", subject_id=budget_id,
        details={"scope": scope, "scope_id": identity, "usd": str(usd), "reason": reason},
    ))
    session.commit()
    return budget_id


def reconcile(session, *, call_id: str, prompt: int, output: int, cached: int,
              request_id: str, evidence: str):
    session.execute(budgets.budget_limits.update().where(
        budgets.budget_limits.c.scope_type == "site",
        budgets.budget_limits.c.scope_id == "default",
        budgets.budget_limits.c.metric == METRIC,
    ).values(quantity=budgets.budget_limits.c.quantity))
    call = session.execute(select(provider_calls).where(
        provider_calls.c.id == call_id,
        provider_calls.c.status.in_(["reserved", "unknown"]),
    )).mappings().first()
    if not call:
        raise ValueError("call is not awaiting reconciliation")
    if min(prompt, output, cached) < 0 or cached > prompt or not evidence.strip():
        raise ValueError("valid provider counts and an evidence reference are required")
    session.execute(budgets.budget_claims.update().where(
        budgets.budget_claims.c.reservation_key == call_id,
        budgets.budget_claims.c.status == "pending_reconciliation",
    ).values(status="reserved"))
    call = dict(call)
    call["settlement_hash"] = None
    session.add(AuditEvent(
        action="budget.reconcile", subject_type="provider_call", subject_id=call_id,
        details={"evidence_reference": evidence, "provider_request_id": request_id},
    ))
    return settle_record(session, call, SettleCall(
        reservation_id=uuid.UUID(call_id), usage={
            "prompt_tokens": prompt, "completion_tokens": output, "cached_tokens": cached,
        }, finish_reason="operator_reconciled", request_id=request_id,
    ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set-limit")
    set_command.add_argument("--scope", choices=["site", "user", "campaign"], required=True)
    set_command.add_argument("--id", required=True)
    set_command.add_argument("--usd", type=Decimal, required=True)
    set_command.add_argument("--days", type=int, default=30)
    set_command.add_argument("--reason", required=True)
    commands.add_parser("list-pending")
    reconcile_command = commands.add_parser("reconcile")
    reconcile_command.add_argument("--call-id", required=True)
    reconcile_command.add_argument("--input-tokens", type=int, required=True)
    reconcile_command.add_argument("--output-tokens", type=int, required=True)
    reconcile_command.add_argument("--cached-tokens", type=int, required=True)
    reconcile_command.add_argument("--request-id", required=True)
    reconcile_command.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get("SAGASMITH_DATABASE_URL")
    if not database_url:
        parser.error("SAGASMITH_DATABASE_URL must be set in the operator environment")
    with make_session_factory(make_engine(database_url))() as session:
        if args.command == "set-limit":
            if args.scope == "site" and args.id != "default":
                parser.error("the site budget id is default")
            print(set_limit(session, scope=args.scope, identity=args.id, usd=args.usd,
                            days=args.days, reason=args.reason))
        elif args.command == "reconcile":
            print(reconcile(session, call_id=args.call_id, prompt=args.input_tokens,
                            output=args.output_tokens, cached=args.cached_tokens,
                            request_id=args.request_id, evidence=args.evidence)["status"])
        else:
            for row in session.execute(select(provider_calls.c.id, provider_calls.c.status,
                                              provider_calls.c.created_at).where(
                provider_calls.c.status.in_(["reserved", "unknown", "overrun"])
            )):
                print(row.id, row.status, row.created_at.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
