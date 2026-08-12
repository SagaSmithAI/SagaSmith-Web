from fastapi.testclient import TestClient

from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.main import create_app
from sagasmith_service.rate_limit import RateLimiterUnavailableError

PASSWORD = "correct horse battery staple"


def registration(index: int) -> dict[str, str]:
    return {
        "email": f"security-{index}@example.com",
        "password": PASSWORD,
        "display_name": f"Security {index}",
    }


def test_authenticated_writes_require_configured_origin(client: TestClient) -> None:
    assert client.post("/api/auth/register", json=registration(1)).status_code == 201
    client.headers.pop("Origin")
    denied_missing = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "csrf-missing-origin"},
        json={"name": "Denied"},
    )
    assert denied_missing.status_code == 403
    denied_cross_site = client.post(
        "/api/campaigns",
        headers={
            "Idempotency-Key": "csrf-cross-site",
            "Origin": "https://attacker.example",
        },
        json={"name": "Denied"},
    )
    assert denied_cross_site.status_code == 403
    allowed = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "csrf-same-origin", "Origin": "http://testserver"},
        json={"name": "Allowed"},
    )
    assert allowed.status_code == 201


def test_auth_rate_limit_returns_retry_after() -> None:
    settings = Settings(
        env="test",
        database_url="sqlite://",
        public_origin="http://testserver",
        auth_rate_limit=2,
        auth_rate_window_seconds=60,
    )
    with TestClient(create_app(settings, make_engine("sqlite://"))) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post("/api/auth/register", json=registration(10)).status_code == 201
        client.cookies.clear()
        assert client.post("/api/auth/register", json=registration(11)).status_code == 201
        client.cookies.clear()
        limited = client.post("/api/auth/register", json=registration(12))
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1


class UnavailableLimiter:
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        raise RateLimiterUnavailableError("offline")


def test_protected_route_fails_closed_when_rate_limiter_is_unavailable() -> None:
    settings = Settings(env="test", database_url="sqlite://")
    app = create_app(
        settings,
        make_engine("sqlite://"),
        rate_limiter=UnavailableLimiter(),
    )
    with TestClient(app) as client:
        response = client.post("/api/auth/register", json=registration(20))
    assert response.status_code == 503
    assert response.headers["X-Content-Type-Options"] == "nosniff"
