import asyncio
import time

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import select, text

from sagasmith_service.api.rooms import _activity_token
from sagasmith_service.database import make_engine
from sagasmith_service.models import AgentRun
from sagasmith_service.observability import (
    DB_REQUEST_SECONDS,
    DB_STATEMENT_SECONDS,
    DB_STATEMENTS_PER_REQUEST,
    EVENT_LOOP_LAG_SECONDS,
    HotPathRequestObservation,
    bind_hot_path_observation,
    hot_path_operation,
    install_database_observability,
    observe_hot_path_request,
    reset_hot_path_observation,
    sample_max_event_loop_lag,
)

_STATEMENT_CLASSES = ("select", "insert", "update", "delete", "transaction", "other")


def _statement_count(operation_class: str, execution_context: str) -> float:
    return sum(
        float(
            REGISTRY.get_sample_value(
                "sagasmith_db_statement_seconds_count",
                {
                    "operation_class": operation_class,
                    "statement_class": statement_class,
                    "execution_context": execution_context,
                    "status": status,
                },
            )
            or 0
        )
        for statement_class in _STATEMENT_CLASSES
        for status in ("success", "error")
    )


def test_hot_path_operation_uses_only_bounded_route_classes() -> None:
    campaign = "sensitive-campaign-id"
    assert (
        hot_path_operation(
            "POST",
            f"/api/campaigns/{campaign}/agent/conversations/conversation-id/messages",
        )
        == "agent_message"
    )
    assert (
        hot_path_operation("POST", f"/api/campaigns/{campaign}/room/messages")
        == "room_action"
    )
    assert (
        hot_path_operation("POST", f"/api/campaigns/{campaign}/room/panel/actions")
        == "room_action"
    )
    assert (
        hot_path_operation("GET", f"/api/campaigns/{campaign}/room/panel")
        == "projection_refresh"
    )
    assert (
        hot_path_operation(
            "POST", f"/api/campaigns/{campaign}/room/internal-activity/run-id"
        )
        == "activity_callback"
    )
    assert hot_path_operation("GET", f"/api/campaigns/{campaign}/room/messages") is None
    assert hot_path_operation("POST", "/api/community/posts") is None


def test_database_observability_distinguishes_event_loop_and_worker_execution() -> None:
    engine = make_engine("sqlite://")
    install_database_observability(engine)

    worker_observation = HotPathRequestObservation("activity_callback")
    worker_token = bind_hot_path_observation(worker_observation)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        reset_hot_path_observation(worker_token)

    event_loop_observation = HotPathRequestObservation("projection_refresh")

    async def execute_on_event_loop() -> None:
        token = bind_hot_path_observation(event_loop_observation)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT 1")) == 1
        finally:
            reset_hot_path_observation(token)

    asyncio.run(execute_on_event_loop())

    assert worker_observation.snapshot()["worker"][1] == 1
    assert worker_observation.snapshot()["event_loop"][1] == 0
    assert event_loop_observation.snapshot()["event_loop"][1] == 1
    assert event_loop_observation.snapshot()["worker"][1] == 0


def test_event_loop_sampler_detects_synchronous_blocking() -> None:
    async def exercise() -> float:
        stop = asyncio.Event()
        started = asyncio.Event()
        sampler = asyncio.create_task(
            sample_max_event_loop_lag(
                stop,
                interval_seconds=0.005,
                started=started,
            )
        )
        await started.wait()
        time.sleep(0.03)
        stop.set()
        return await sampler

    assert asyncio.run(exercise()) >= 0.02


def test_hot_path_metric_labels_remain_bounded() -> None:
    assert EVENT_LOOP_LAG_SECONDS._labelnames == ("operation_class", "status")
    assert DB_STATEMENT_SECONDS._labelnames == (
        "operation_class",
        "statement_class",
        "execution_context",
        "status",
    )
    assert DB_REQUEST_SECONDS._labelnames == (
        "operation_class",
        "execution_context",
        "status",
    )
    assert DB_STATEMENTS_PER_REQUEST._labelnames == DB_REQUEST_SECONDS._labelnames

    observation = HotPathRequestObservation("agent_message")
    observation.record(execution_context="event_loop", elapsed=0.001)
    observe_hot_path_request(
        observation,
        status="success",
        event_loop_lag_seconds=0.002,
    )


def test_agent_message_metrics_capture_worker_auth_and_event_loop_handler_db(
    client: TestClient,
) -> None:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "hotpath-agent@example.com",
            "password": "correct horse battery staple",
            "display_name": "Hot path Agent",
        },
    )
    assert registered.status_code == 201
    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "hotpath-agent-campaign"},
        json={"name": "Hot path campaign"},
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()["id"]
    conversation = client.post(
        f"/api/campaigns/{campaign_id}/agent/conversations",
        json={"title": "Hot path"},
    )
    assert conversation.status_code == 201
    before_worker = _statement_count("agent_message", "worker")
    before_event_loop = _statement_count("agent_message", "event_loop")

    response = client.post(
        f"/api/campaigns/{campaign_id}/agent/conversations/"
        f"{conversation.json()['id']}/messages",
        headers={"Idempotency-Key": "hotpath-agent-message"},
        json={"content": "Measure this request."},
    )

    assert response.status_code == 200
    assert _statement_count("agent_message", "worker") > before_worker
    assert _statement_count("agent_message", "event_loop") > before_event_loop


def test_activity_callback_metrics_keep_database_work_off_event_loop(
    client: TestClient,
) -> None:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "hotpath-activity@example.com",
            "password": "correct horse battery staple",
            "display_name": "Hot path activity",
        },
    )
    assert registered.status_code == 201
    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "hotpath-activity-campaign"},
        json={"name": "Hot path activity campaign"},
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()["id"]
    action = client.post(
        f"/api/campaigns/{campaign_id}/room/messages",
        headers={"Idempotency-Key": "hotpath-activity-seed"},
        json={"content": "Seed callback.", "mode": "action"},
    )
    assert action.status_code == 200
    with client.app.state.session_factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.campaign_id == campaign_id))
        assert run is not None
        run.status = "running"
        run_id = run.id
        session.commit()
    token = _activity_token(client.app.state.settings.session_secret, campaign_id, run_id)
    before_worker = _statement_count("activity_callback", "worker")
    before_event_loop = _statement_count("activity_callback", "event_loop")

    response = client.post(
        f"/api/campaigns/{campaign_id}/room/internal-activity/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "schema": "sagasmith.room-activity/v1",
            "run_id": run_id,
            "activity_id": "hotpath-callback",
            "audience": {"kind": "public"},
            "code": "reviewing_rules",
            "state": "started",
        },
    )

    assert response.status_code == 200
    assert _statement_count("activity_callback", "worker") > before_worker
    assert _statement_count("activity_callback", "event_loop") == before_event_loop
