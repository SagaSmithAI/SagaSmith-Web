from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sagasmith_service.database import make_session_factory
from sagasmith_service.models import QuotaGrant, User, now_utc
from sagasmith_service.quota import QuotaExceededError, balance, release, reserve, settle


def test_signup_grant_is_visible(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "quota@example.com",
            "password": "correct horse battery staple",
            "display_name": "Quota User",
        },
    )
    response = client.get("/api/usage/balance")
    assert response.status_code == 200
    assert response.json() == {
        "metric": "llm_tokens",
        "granted": "1000000.000000",
        "used": "0.000000",
        "reserved": "0.000000",
        "available": "1000000.000000",
    }


def test_reserve_settle_release_and_idempotency(client: TestClient) -> None:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "ledger@example.com",
            "password": "correct horse battery staple",
            "display_name": "Ledger User",
        },
    ).json()
    factory = make_session_factory(client.app.state.engine)
    with factory() as session:
        reservation = reserve(
            session,
            user_id=registered["user"]["id"],
            campaign_id=None,
            metric="llm_tokens",
            quantity=Decimal("100"),
            idempotency_key="reserve-1",
        )
        same = reserve(
            session,
            user_id=registered["user"]["id"],
            campaign_id=None,
            metric="llm_tokens",
            quantity=Decimal("100"),
            idempotency_key="reserve-1",
        )
        assert same.id == reservation.id
        usage = settle(
            session,
            reservation_id=reservation.id,
            quantity=Decimal("72"),
            idempotency_key="settle-1",
            unit="tokens",
        )
        same_usage = settle(
            session,
            reservation_id=reservation.id,
            quantity=Decimal("72"),
            idempotency_key="settle-1",
            unit="tokens",
        )
        assert same_usage.id == usage.id
        session.commit()
        current = balance(session, registered["user"]["id"], "llm_tokens")
        assert current.used == Decimal("72.000000")
        assert current.reserved == 0

        second = reserve(
            session,
            user_id=registered["user"]["id"],
            campaign_id=None,
            metric="llm_tokens",
            quantity=Decimal("50"),
            idempotency_key="reserve-2",
        )
        release(session, second.id)
        session.commit()
        assert balance(session, registered["user"]["id"], "llm_tokens").reserved == 0


def test_quota_cannot_be_overdrawn(client: TestClient) -> None:
    user_id = client.post(
        "/api/auth/register",
        json={
            "email": "limited@example.com",
            "password": "correct horse battery staple",
            "display_name": "Limited",
        },
    ).json()["user"]["id"]
    factory = make_session_factory(client.app.state.engine)
    with factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        grants = session.query(QuotaGrant).filter_by(user_id=user_id).all()
        for grant in grants:
            grant.period_end = now_utc()
        session.commit()
        with pytest.raises(QuotaExceededError):
            reserve(
                session,
                user_id=user_id,
                campaign_id=None,
                metric="llm_tokens",
                quantity=Decimal("1"),
                idempotency_key="overdraw",
            )
