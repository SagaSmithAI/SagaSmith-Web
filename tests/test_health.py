from fastapi.testclient import TestClient

from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.main import create_app


def test_health_reports_current_service() -> None:
    settings = Settings(env="test", database_url="sqlite://")
    client = TestClient(create_app(settings, make_engine("sqlite://")))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_web_shell() -> None:
    settings = Settings(env="test", database_url="sqlite://")
    client = TestClient(create_app(settings, make_engine("sqlite://")))
    response = client.get("/")
    assert response.status_code == 200
    assert "SagaSmith" in response.text
    assert "SAGASMITH FORGE" in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_readiness_metrics_and_request_id(dnd_runtime, agent_runtime, tmp_path) -> None:
    settings = Settings(env="test", database_url="sqlite://")
    client = TestClient(
        create_app(
            settings,
            make_engine("sqlite://"),
            dnd_runtime,
            agent_runtime,
            coc_runtime=dnd_runtime,
            narrative_runtime=dnd_runtime,
        )
    )
    ready = client.get("/api/ready", headers={"X-Request-ID": "test-request-123"})
    assert ready.status_code == 200
    assert set(ready.json()["components"]) == {
        "database",
        "dnd_mcp",
        "coc_mcp",
        "narrative_mcp",
        "agent",
        "rate_limiter",
        "private_storage",
    }
    assert ready.headers["X-Request-ID"] == "test-request-123"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "sagasmith_http_requests_total" in metrics.text


def test_readiness_rejects_an_unavailable_required_component(
    dnd_runtime, agent_runtime, tmp_path
) -> None:
    dnd_runtime.fail_probe = True
    settings = Settings(
        env="test",
        database_url="sqlite://",
        private_storage_dir=str(tmp_path / "private"),
        exchange_dir=str(tmp_path / "exchange"),
    )
    client = TestClient(
        create_app(
            settings,
            make_engine("sqlite://"),
            dnd_runtime,
            agent_runtime,
            coc_runtime=dnd_runtime,
            narrative_runtime=dnd_runtime,
        )
    )

    ready = client.get("/api/ready")

    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["components"]["dnd_mcp"] == "not_ready"
