from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.database import Base, make_engine, make_session_factory
from sagasmith_service.models import AuditEvent, User, UserSession, now_utc
from sagasmith_service.security import SESSION_COOKIE, create_session

SESSION_ACTIVITY_SENTINEL = datetime(2000, 1, 1, tzinfo=UTC)


def register(client: TestClient, email: str = "dm@example.com"):
    return client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": "Dungeon Master",
            "terms_accepted": True,
            "terms_version": "2026-08-29",
            "privacy_version": "2026-08-29",
        },
    )


def test_register_me_logout_and_login(client: TestClient) -> None:
    created = register(client)
    assert created.status_code == 201
    assert created.json()["user"]["principal_id"].startswith("user:")
    assert client.get("/api/auth/me").status_code == 200

    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    logged_in = client.post(
        "/api/auth/login",
        json={
            "email": "DM@EXAMPLE.COM",
            "password": "correct-horse-battery-staple",
        },
    )
    assert logged_in.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_duplicate_registration_and_wrong_password_are_bounded(client: TestClient) -> None:
    assert register(client).status_code == 201
    assert register(client).status_code == 409

    rejected = client.post(
        "/api/auth/login",
        json={"email": "dm@example.com", "password": "not-the-password"},
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid email or password"


def test_registration_requires_current_legal_acceptance(client: TestClient) -> None:
    payload = {
        "email": "legal@example.com",
        "password": "correct-horse-battery-staple",
        "display_name": "Legal Reader",
        "terms_accepted": False,
        "terms_version": "2026-08-29",
        "privacy_version": "2026-08-29",
    }
    assert client.post("/api/auth/register", json=payload).status_code == 422
    payload["terms_accepted"] = True
    payload["terms_version"] = "stale"
    assert client.post("/api/auth/register", json=payload).status_code == 422


def test_profile_and_session_controls(client: TestClient) -> None:
    assert register(client).status_code == 201
    factory = client.app.state.session_factory
    with factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "dm@example.com"))
        assert user is not None
        extra, _ = create_session(session, user, ttl_seconds=3600)
        extra_id = extra.id

    updated = client.patch("/api/auth/me", json={"display_name": "  Table Keeper  "})
    assert updated.status_code == 200
    assert updated.json()["user"]["display_name"] == "Table Keeper"
    assert client.patch("/api/auth/me", json={"display_name": "   "}).status_code == 422

    sessions = client.get("/api/auth/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()) == 2
    assert sum(item["current"] for item in sessions.json()) == 1

    revoked = client.post("/api/auth/sessions/revoke-others")
    assert revoked.status_code == 204
    with factory() as session:
        assert session.get(UserSession, extra_id).revoked_at is not None


def test_password_change_rotates_every_session(client: TestClient) -> None:
    assert register(client).status_code == 201
    old_token = client.cookies.get(SESSION_COOKIE)
    assert old_token

    rejected = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "a-new-secure-passphrase"},
    )
    assert rejected.status_code == 401
    same = client.post(
        "/api/auth/password",
        json={
            "current_password": "correct-horse-battery-staple",
            "new_password": "correct-horse-battery-staple",
        },
    )
    assert same.status_code == 409

    changed = client.post(
        "/api/auth/password",
        json={
            "current_password": "correct-horse-battery-staple",
            "new_password": "a-new-secure-passphrase",
        },
    )
    assert changed.status_code == 200
    new_token = client.cookies.get(SESSION_COOKIE)
    assert new_token and new_token != old_token

    client.cookies.set(SESSION_COOKIE, old_token)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set(SESSION_COOKIE, new_token)
    assert client.get("/api/auth/me").status_code == 200

    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "dm@example.com", "password": "correct-horse-battery-staple"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "dm@example.com", "password": "a-new-secure-passphrase"},
        ).status_code
        == 200
    )


def test_deactivation_revokes_access_and_is_audited(client: TestClient) -> None:
    assert register(client).status_code == 201

    rejected = client.post(
        "/api/auth/deactivate",
        json={"current_password": "wrong", "confirmation": "DEACTIVATE"},
    )
    assert rejected.status_code == 401
    deactivated = client.post(
        "/api/auth/deactivate",
        json={
            "current_password": "correct-horse-battery-staple",
            "confirmation": "DEACTIVATE",
        },
    )
    assert deactivated.status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={
                "email": "dm@example.com",
                "password": "correct-horse-battery-staple",
            },
        ).status_code
        == 401
    )

    factory = client.app.state.session_factory
    with factory() as session:
        user = session.scalar(select(User).where(User.email == "dm@example.com"))
        assert user is not None and user.status == "deactivated"
        assert session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "account.deactivate")
        ) == 1


def test_authenticated_read_persists_session_activity(client: TestClient) -> None:
    assert register(client).status_code == 201
    factory = client.app.state.session_factory
    with factory.begin() as session:
        active = session.scalar(select(UserSession))
        assert active is not None
        active.last_seen_at = SESSION_ACTIVITY_SENTINEL
        active_session_id = active.id

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    with factory() as session:
        refreshed = session.get(UserSession, active_session_id)
        assert refreshed is not None
        assert refreshed.last_seen_at > SESSION_ACTIVITY_SENTINEL.replace(tzinfo=None)


@pytest.mark.parametrize("rejection", ["invalid", "expired", "revoked"])
def test_rejected_session_does_not_refresh_activity(
    client: TestClient, rejection: str
) -> None:
    assert register(client).status_code == 201
    factory = client.app.state.session_factory
    with factory.begin() as session:
        active = session.scalar(select(UserSession))
        assert active is not None
        active.last_seen_at = SESSION_ACTIVITY_SENTINEL
        active_session_id = active.id
        if rejection == "expired":
            active.expires_at = now_utc() - timedelta(seconds=1)
        elif rejection == "revoked":
            active.revoked_at = now_utc()
    if rejection == "invalid":
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, "not-a-valid-session-token")

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    with factory() as session:
        unchanged = session.get(UserSession, active_session_id)
        assert unchanged is not None
        assert unchanged.last_seen_at == SESSION_ACTIVITY_SENTINEL.replace(tzinfo=None)


def test_route_exception_does_not_commit_unrelated_request_state() -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        user = User(
            email="route-error@example.com",
            password_hash="not-used",
            display_name="Route Error",
        )
        session.add(user)
        session.flush()
        active, token = create_session(session, user, ttl_seconds=3600)
        active.last_seen_at = SESSION_ACTIVITY_SENTINEL
        active_session_id = active.id

    app = FastAPI()
    app.state.session_factory = factory

    @app.get("/fails-after-authentication")
    def fails_after_authentication(user: CurrentUser, session: DbSession) -> None:
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="test.must_rollback",
                subject_type="user",
                subject_id=user.id,
            )
        )
        raise HTTPException(status_code=500, detail="expected test failure")

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        response = client.get("/fails-after-authentication")

    assert response.status_code == 500
    with factory() as session:
        refreshed = session.get(UserSession, active_session_id)
        assert refreshed is not None
        assert refreshed.last_seen_at > SESSION_ACTIVITY_SENTINEL.replace(tzinfo=None)
        assert session.scalar(select(func.count(AuditEvent.id))) == 0
    engine.dispose()
