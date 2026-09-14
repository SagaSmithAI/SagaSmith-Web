import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from sagasmith_service.budgets import budget_limits
from sagasmith_service.models import now_utc
from sagasmith_service.provider_budget_api import METRIC, callback_for, provider_calls
from sagasmith_service.usage_accounting import provider_consumption_ledger


def configure(client):
    result = client.post("/api/auth/register", json={
        "email": "budget@example.com", "password": "correct horse battery staple",
        "display_name": "Budget tester",
    })
    user_id = result.json()["user"]["id"]
    settings = client.app.state.settings
    settings.provider_budget_enabled = True
    from sagasmith_service.config import ProviderPrice
    settings.provider_prices = {"test-model": ProviderPrice(
        provider="OpenAICompatProvider", version="test-only-v1", input_usd_per_million="1",
        valid_until=now_utc() + timedelta(days=1),
        cached_usd_per_million="0.1", output_usd_per_million="2", max_input_tokens=1000,
        max_output_tokens=100, max_request_bytes=4000,
    )}
    now = now_utc()
    with client.app.state.session_factory.begin() as session:
        for scope, identity in [("site", "default"), ("user", user_id), ("campaign", "campaign")]:
            session.execute(budget_limits.insert().values(
                id=str(uuid.uuid4()), scope_type=scope, scope_id=identity, metric=METRIC,
                quantity=Decimal("0.002"), period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=30), status="active", created_at=now,
            ))
    callback = callback_for(settings, {"authority_context": {
        "requester_principal": f"user:{user_id}", "campaign_id": "campaign", "room_turn_id": "task",
    }})
    client.cookies.clear()
    return {"Authorization": f"Bearer {callback['token']}"}


def attempt(client, headers):
    call_id = str(uuid.uuid4())
    result = client.post("/internal/provider-budget/authorize", headers=headers, json={
        "call_id": call_id, "provider": "OpenAICompatProvider", "model": "test-model",
        "max_output_tokens": 100, "request_bytes": 1000,
    })
    return call_id, result


def test_cost_ledger_settlement_idempotency_and_budget_stop(client):
    headers = configure(client)
    call_id, result = attempt(client, headers)
    assert result.status_code == 200, result.text
    payload = {"reservation_id": call_id, "usage": {
        "prompt_tokens": 1000, "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 500},
    }, "finish_reason": "stop", "request_id": "provider-request-1"}
    for _ in range(2):
        settled = client.post("/internal/provider-budget/settle", headers=headers, json=payload)
        assert settled.status_code == 200, settled.text
    with client.app.state.session_factory() as session:
        rows = session.execute(select(provider_consumption_ledger).where(
            provider_consumption_ledger.c.metric == METRIC
        )).mappings().all()
        assert len(rows) == 1
        assert rows[0]["quantity"] == Decimal("0.000750")
        assert rows[0]["pricing_cache"]["tokens"] == 500
    payload["usage"]["completion_tokens"] = 99
    assert client.post(
        "/internal/provider-budget/settle", headers=headers, json=payload
    ).status_code == 409
    second, result = attempt(client, headers)
    assert result.status_code == 200
    assert client.post("/internal/provider-budget/settle", headers=headers, json={
        "reservation_id": second, "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        "finish_reason": "stop",
    }).status_code == 200
    assert attempt(client, headers)[1].status_code == 402


def test_unknown_outcome_stays_reserved_and_prevents_retry(client):
    headers = configure(client)
    call_id, result = attempt(client, headers)
    assert result.status_code == 200
    assert attempt(client, headers)[1].status_code == 409
    result = client.post("/internal/provider-budget/settle", headers=headers, json={
        "reservation_id": call_id, "usage": {}, "finish_reason": "error",
    })
    assert result.json()["status"] == "unknown"
    assert attempt(client, headers)[1].status_code == 409
    with client.app.state.session_factory() as session:
        assert session.scalar(select(provider_calls.c.status).where(
            provider_calls.c.id == call_id
        )) == "unknown"


def test_callback_requires_host_credential_and_reviewed_model(client):
    headers = configure(client)
    assert attempt(client, {})[1].status_code == 401
    client.app.state.settings.provider_prices = {}
    assert attempt(client, headers)[1].status_code == 403


def test_offline_reconciliation_releases_unknown_claim_without_losing_cost(client):
    from sagasmith_service.budget_cli import reconcile

    headers = configure(client)
    call_id, result = attempt(client, headers)
    assert result.status_code == 200
    client.post("/internal/provider-budget/settle", headers=headers, json={
        "reservation_id": call_id, "usage": {}, "finish_reason": "error",
    })
    with client.app.state.session_factory() as session:
        assert reconcile(
            session, call_id=call_id, prompt=100, output=10, cached=50,
            request_id="real-provider-id", evidence="private-invoice-line",
        )["status"] == "settled"
    assert attempt(client, headers)[1].status_code == 200


def test_expired_prices_never_authorize(client):
    headers = configure(client)
    client.app.state.settings.provider_prices["test-model"].valid_until = (
        now_utc() - timedelta(seconds=1)
    )
    assert attempt(client, headers)[1].status_code == 403
