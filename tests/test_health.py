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


def test_readiness_metrics_and_request_id() -> None:
    settings = Settings(env="test", database_url="sqlite://")
    client = TestClient(create_app(settings, make_engine("sqlite://")))
    ready = client.get("/api/ready", headers={"X-Request-ID": "test-request-123"})
    assert ready.status_code == 200
    assert ready.headers["X-Request-ID"] == "test-request-123"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "sagasmith_http_requests_total" in metrics.text
