from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert

from sagasmith_service import budgets
from sagasmith_service.budgets import (
    BudgetClaimClosedError,
    BudgetClaimPendingError,
    BudgetExceededError,
    BudgetUnavailableError,
    admit,
    budget_limits,
)
from sagasmith_service.database import make_session_factory
from sagasmith_service.models import QuotaGrant, UsageLedger, now_utc
from sagasmith_service.quota import balance, reserve, settle
from sagasmith_service.usage_accounting import record_provider_consumption


def _user(client, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "display_name": "Billing Test",
        },
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["user"]["id"]
    with client.app.state.session_factory() as session:
        start = now_utc()
        session.add(
            QuotaGrant(
                user_id=user_id,
                metric="llm_tokens",
                quantity=Decimal("100"),
                period_start=start,
                period_end=start + timedelta(days=30),
                source="test",
            )
        )
        session.commit()
    return user_id


def test_balance_only_consumes_current_grant_period(client) -> None:
    user_id = _user(client, "periods@example.com")
    factory = make_session_factory(client.app.state.engine)
    with factory() as session:
        current = now_utc()
        old = QuotaGrant(
            user_id=user_id,
            metric="llm_tokens",
            quantity=Decimal("500"),
            period_start=current - timedelta(days=60),
            period_end=current - timedelta(days=30),
            source="purchased",
        )
        permanent = QuotaGrant(
            user_id=user_id,
            metric="llm_tokens",
            quantity=Decimal("200"),
            period_start=current - timedelta(days=1),
            period_end=current + timedelta(days=3650),
            source="permanent",
        )
        session.add_all([old, permanent])
        session.flush()
        session.add(
            UsageLedger(
                user_id=user_id,
                metric="llm_tokens",
                quantity=Decimal("500"),
                unit="tokens",
                idempotency_key="old-period-usage",
                occurred_at=current - timedelta(days=45),
                details={"grant_allocations": [{"grant_id": old.id, "quantity": "500"}]},
            )
        )
        session.commit()
        current_balance = balance(session, user_id, "llm_tokens")
        assert current_balance.granted == Decimal("300")
        assert current_balance.used == Decimal("0")
        assert current_balance.available == Decimal("300")


def test_provider_overrun_is_uncapped_but_entitlement_stays_reserved(client) -> None:
    user_id = _user(client, "overrun@example.com")
    factory = make_session_factory(client.app.state.engine)
    with factory() as session:
        reservation = reserve(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            metric="llm_tokens",
            quantity=Decimal("10"),
            idempotency_key="overrun-reserve",
        )
        provider = record_provider_consumption(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            task_id="task-1",
            reservation_id=reservation.id,
            metric="llm_tokens",
            quantity=Decimal("25"),
            unit="tokens",
            idempotency_key="provider-overrun",
            provider="test-provider",
            model="test-model",
            request_id="request-1",
        )
        entitlement = settle(
            session,
            reservation_id=reservation.id,
            quantity=Decimal("10"),
            idempotency_key="overrun-settle",
            unit="tokens",
            provider="test-provider",
            model="test-model",
            request_id="request-1",
            details={"provider_quantity": "25"},
        )
        assert provider.quantity == Decimal("25")
        assert entitlement.quantity == Decimal("10")
        assert balance(session, user_id, "llm_tokens").used == Decimal("10")
        same_provider = record_provider_consumption(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            task_id="task-1",
            reservation_id=reservation.id,
            metric="llm_tokens",
            quantity=Decimal("25"),
            unit="tokens",
            idempotency_key="provider-overrun",
        )
        assert same_provider.id == provider.id


def test_budget_admission_is_fail_closed_and_idempotent(client) -> None:
    user_id = _user(client, "budgets@example.com")
    factory = make_session_factory(client.app.state.engine)
    with factory() as session:
        with pytest.raises(BudgetUnavailableError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-1",
                metric="llm_tokens",
                quantity=Decimal("10"),
                reservation_key="budget-1",
            )
        start = now_utc() - timedelta(minutes=1)
        end = now_utc() + timedelta(hours=1)
        for scope_type, scope_id in (
            ("site", "default"),
            ("user", user_id),
            ("campaign", "campaign-1"),
            ("task", "task-1"),
            ("task", "task-2"),
            ("task", "task-3"),
        ):
            session.execute(
                insert(budget_limits).values(
                    id=f"{scope_type}-{scope_id}-budget-1",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    metric="llm_tokens",
                    quantity=Decimal("10"),
                    period_start=start,
                    period_end=end,
                    source="test",
                    created_at=start,
                )
            )
        session.flush()
        admitted = admit(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            task_id="task-1",
            metric="llm_tokens",
            quantity=Decimal("10"),
            reservation_key="budget-1",
        )
        assert len(admitted) == 4
        same = admit(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            task_id="task-1",
            metric="llm_tokens",
            quantity=Decimal("10"),
            reservation_key="budget-1",
        )
        assert len(same) == 4
        with pytest.raises(ValueError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-1",
                metric="llm_tokens",
                quantity=Decimal("9"),
                reservation_key="budget-1",
            )
        with pytest.raises(BudgetExceededError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-2",
                metric="llm_tokens",
                quantity=Decimal("1"),
                reservation_key="budget-2",
            )
        budgets.settle(session, "budget-1", Decimal("25"))
        budgets.release(session, "budget-1")
        session.flush()
        admit(
            session,
            user_id=user_id,
            campaign_id="campaign-1",
            task_id="task-3",
            metric="llm_tokens",
            quantity=Decimal("1"),
            reservation_key="budget-3",
        )
        budgets.mark_unknown(session, "budget-3", "provider timeout")
        with pytest.raises(BudgetClaimPendingError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-3",
                metric="llm_tokens",
                quantity=Decimal("1"),
                reservation_key="budget-3",
            )
        budgets.reconcile(session, "budget-3", Decimal("1"))
        with pytest.raises(BudgetClaimClosedError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-3",
                metric="llm_tokens",
                quantity=Decimal("1"),
                reservation_key="budget-3",
            )
        session.commit()
        with pytest.raises(BudgetClaimClosedError):
            admit(
                session,
                user_id=user_id,
                campaign_id="campaign-1",
                task_id="task-1",
                metric="llm_tokens",
                quantity=Decimal("10"),
                reservation_key="budget-1",
            )
        assert (
            session.execute(
                budget_limits.select().where(budget_limits.c.scope_type == "site")
            ).first()
            is not None
        )
