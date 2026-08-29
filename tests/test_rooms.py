import asyncio
import base64
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from sagasmith_service.api.dependencies import get_async_db
from sagasmith_service.api.rooms import _activity_token
from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.integrations.agent import AgentRuntimeError
from sagasmith_service.main import create_app
from sagasmith_service.models import (
    AgentRun,
    AuditEvent,
    CampaignMembershipProjection,
    CampaignMessage,
    CampaignRoomEvent,
    CampaignSuggestion,
    OutboxEvent,
    QuotaReservation,
    RoomMediaArtifact,
    RoomTurnJob,
    UserSession,
    now_utc,
)
from sagasmith_service.quota import balance
from sagasmith_service.room_jobs import RoomTurnJobProcessor
from sagasmith_service.security import SESSION_COOKIE

PASSWORD = "correct horse battery staple"


def register(client: TestClient, email: str, name: str) -> dict[str, Any]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": name},
    )
    assert response.status_code == 201
    return response.json()["user"]


def login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def create_campaign(client: TestClient) -> None:
    response = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "room-campaign-create"},
        json={"name": "Shared Room"},
    )
    assert response.status_code == 201


def add_player(client: TestClient, email: str, name: str) -> dict[str, Any]:
    player = register(client, email, name)
    requested = client.post("/api/campaigns/campaign-1/join-requests", json={}).json()
    login(client, "room-owner@example.com")
    approved = client.post(
        f"/api/campaigns/campaign-1/join-requests/{requested['id']}/decision",
        json={"decision": "approved"},
    )
    assert approved.status_code == 200
    login(client, email)
    return player


def test_combat_render_is_revision_cached_revalidated_and_membership_scoped(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)

    rendered = client.get("/api/campaigns/campaign-1/room/combat/render")

    expected = b"\x89PNG\r\n\x1a\nparty-public-combat"
    assert rendered.status_code == 200
    assert rendered.content == expected
    assert rendered.headers["content-type"] == "image/png"
    assert rendered.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert rendered.headers["x-content-type-options"] == "nosniff"
    assert rendered.headers["etag"] == f'"{hashlib.sha256(expected).hexdigest()}"'
    assert rendered.headers["x-sagasmith-combat-revision"] == "1"
    assert rendered.headers["x-sagasmith-combat-projection"] == "party_public"
    assert rendered.headers["x-sagasmith-combat-renderer"] == "dnd-party-public-v1"
    assert rendered.headers["x-sagasmith-combat-artifact"].startswith(
        "campaign-1:1:party_public:default:native:"
    )
    assert (
        base64.urlsafe_b64decode(rendered.headers["x-sagasmith-combat-alt"] + "==").decode()
        == "石厅战斗网格；Aria 当前行动。"
    )
    assert (
        base64.urlsafe_b64decode(rendered.headers["x-sagasmith-combat-caption"] + "==").decode()
        == "Aria 在石厅迎战敌人。"
    )
    call = next(item for item in dnd_runtime.calls if item[0] == "combat_render_public")
    assert call[1] == {
        "campaign_id": "campaign-1",
        "principal_id": f"user:{owner['id']}",
    }
    not_modified = client.get(
        "/api/campaigns/campaign-1/room/combat/render",
        headers={"If-None-Match": f'"unrelated", W/{rendered.headers["etag"]}'},
    )
    assert not_modified.status_code == 304
    assert not not_modified.content
    assert len([item for item in dnd_runtime.calls if item[0] == "combat_render_public"]) == 1

    register(client, "room-outsider@example.com", "Outsider")
    forbidden = client.get("/api/campaigns/campaign-1/room/combat/render")
    assert forbidden.status_code == 403
    assert len([item for item in dnd_runtime.calls if item[0] == "combat_render_public"]) == 1


def test_room_is_shared_and_agent_receives_sender_visible_timeline(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    room = client.get("/api/campaigns/campaign-1/room")
    assert room.status_code == 200
    snapshot = client.get("/api/campaigns/campaign-1/room/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["room"]["id"] == room.json()["id"]
    assert snapshot.json()["messages"] == []
    assert snapshot.json()["event_cursor"] == 0

    first = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "room-chat-message-1"},
        json={"content": "大家先在门外集合。", "mode": "chat"},
    )
    assert first.status_code == 200
    assert first.json()["agent_message"] is None

    player = add_player(client, "room-player@example.com", "Aria")
    visible = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert [item["content"] for item in visible] == ["大家先在门外集合。"]
    snapshot = client.get("/api/campaigns/campaign-1/room/snapshot").json()
    assert [item["content"] for item in snapshot["messages"]] == ["大家先在门外集合。"]
    assert snapshot["event_cursor"] == 1

    action = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "room-action-message-1"},
        json={
            "content": "我检查门锁。",
            "mode": "action",
            "structured_payload": {
                "actor_id": "actor-1",
                "target_id": "door-1",
                "grid": {"destination": {"x": 4, "y": 2}, "positioning_mode": "grid"},
            },
        },
    )
    assert action.status_code == 200, action.text
    assert action.json()["agent_message"]["content"] == agent_runtime.content
    call = agent_runtime.calls[-1]
    assert call["context"]["principal_id"] == f"user:{player['id']}"
    assert call["context"]["room_id"] == room.json()["id"]
    assert call["context"]["action_context"] == {
        "actor_id": "actor-1",
        "target_id": "door-1",
        "grid": {"destination": {"x": 4, "y": 2}, "positioning_mode": "grid"},
    }
    assert [item["content"] for item in call["context"]["room_context"]] == [
        "大家先在门外集合。",
        "我检查门锁。",
    ]

    repeated = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "room-action-message-1"},
        json={
            "content": "我检查门锁。",
            "mode": "action",
            "structured_payload": {
                "actor_id": "actor-1",
                "target_id": "door-1",
                "grid": {"destination": {"x": 4, "y": 2}, "positioning_mode": "grid"},
            },
        },
    )
    assert repeated.status_code == 200
    assert len(agent_runtime.calls) == 1

    login(client, "room-owner@example.com")
    timeline = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert [item["sender_type"] for item in timeline] == ["user", "user", "agent"]
    assert timeline[1]["sender_user_id"] == player["id"]
    assert timeline[2]["trigger_message_id"] == timeline[1]["id"]
    assert timeline[0]["sender_user_id"] == owner["id"]


def test_concurrent_room_action_retries_share_one_agent_run(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    entered_agent = threading.Event()
    release_agent = threading.Event()
    original_complete = agent_runtime.complete

    async def delayed_complete(**arguments: Any):
        entered_agent.set()
        while not release_agent.is_set():
            await asyncio.sleep(0.01)
        return await original_complete(**arguments)

    agent_runtime.complete = delayed_complete
    start = threading.Barrier(3)

    def post_retry():
        start.wait()
        return client.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": "concurrent-room-retry"},
            json={"content": "I inspect the same lock.", "mode": "action"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(post_retry) for _ in range(2)]
        start.wait()
        assert entered_agent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not any(future.done() for future in futures) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert any(future.done() for future in futures), (
            "an idempotent retry should not wait for the in-flight Agent call"
        )
        release_agent.set()
        responses = [future.result(timeout=5) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    assert len(agent_runtime.calls) == 1
    with client.app.state.session_factory() as session:
        messages = session.scalars(
            select(CampaignMessage).where(
                CampaignMessage.client_message_id == "concurrent-room-retry"
            )
        ).all()
        runs = session.scalars(
            select(AgentRun).where(AgentRun.idempotency_key == "room:concurrent-room-retry")
        ).all()
    assert len(messages) == 1
    assert len(runs) == 1


def test_different_players_run_agent_work_concurrently_and_settle_in_room_order(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "parallel-player@example.com", "Player")
    player_cookie = client.cookies.get(SESSION_COOKIE)
    login(client, "room-owner@example.com")
    owner_cookie = client.cookies.get(SESSION_COOKIE)
    assert owner_cookie and player_cookie
    original_complete = agent_runtime.complete
    release = threading.Event()
    both_entered = threading.Event()
    active = 0
    maximum = 0

    async def delayed_complete(**arguments: Any):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if maximum == 2:
            both_entered.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original_complete(**arguments)
        finally:
            active -= 1

    agent_runtime.complete = delayed_complete

    def post_action(key: str, content: str, cookie: str):
        return client.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={
                "Idempotency-Key": key,
                "Cookie": f"{SESSION_COOKIE}={cookie}",
            },
            json={"content": content, "mode": "action"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(post_action, "parallel-owner", "Owner acts.", owner_cookie),
            executor.submit(post_action, "parallel-player", "Player acts.", player_cookie),
        ]
        try:
            assert both_entered.wait(timeout=5)
        finally:
            release.set()
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    assert maximum == 2
    assert len(agent_runtime.calls) == 2
    with client.app.state.session_factory() as session:
        jobs = session.scalars(
            select(RoomTurnJob).where(
                RoomTurnJob.idempotency_key.in_(("parallel-owner", "parallel-player"))
            )
        ).all()
        assert {job.user_id for job in jobs} == {owner["id"], player["id"]}
        assert {job.status for job in jobs} == {"succeeded"}


def test_configured_per_room_scheduler_limits_one_room_without_holding_database(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "scheduled-player@example.com", "Player")
    player_cookie = client.cookies.get(SESSION_COOKIE)
    login(client, "room-owner@example.com")
    owner_cookie = client.cookies.get(SESSION_COOKIE)
    assert player["id"] and owner_cookie and player_cookie
    processor = client.app.state.room_turn_jobs
    processor.per_room_concurrency = 1
    original_complete = agent_runtime.complete
    first_entered = threading.Event()
    release_first = threading.Event()
    active = 0
    maximum = 0

    async def delayed_complete(**arguments: Any):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        first_entered.set()
        try:
            while not release_first.is_set():
                await asyncio.sleep(0.01)
            return await original_complete(**arguments)
        finally:
            active -= 1

    agent_runtime.complete = delayed_complete

    def post_action(key: str, cookie: str):
        return client.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": key, "Cookie": f"{SESSION_COOKIE}={cookie}"},
            json={"content": key, "mode": "action"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(post_action, "scheduled-owner", owner_cookie),
            executor.submit(post_action, "scheduled-player", player_cookie),
        ]
        assert first_entered.wait(timeout=5)
        time.sleep(0.1)
        assert maximum == 1
        assert not processor._transaction_lock.locked()
        release_first.set()
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    assert maximum == 1


def test_per_room_scheduler_limit_is_shared_across_processors(
    client: TestClient,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    keys = ("shared-scheduler-one", "shared-scheduler-two")
    for key in keys:
        response = client.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": key},
            json={"content": key, "mode": "action"},
        )
        assert response.status_code == 200, response.text

    original = client.app.state.room_turn_jobs
    client.portal.call(original.close)
    with original.factory() as session:
        jobs = session.scalars(
            select(RoomTurnJob).where(RoomTurnJob.idempotency_key.in_(keys))
        ).all()
        assert len(jobs) == 2
        for job in jobs:
            job.status = "queued"
            job.attempt = 0
            job.available_at = now_utc()
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.completed_at = None
        session.commit()

    async def unused_executor(_job_id: str) -> None:
        raise AssertionError("claim tests must not execute a turn")

    def independent_processor(worker_id: str) -> RoomTurnJobProcessor:
        return RoomTurnJobProcessor(
            original.factory,
            unused_executor,
            concurrency=1,
            poll_seconds=0.01,
            lease_seconds=30,
            per_room_concurrency=1,
            reservation_ttl_seconds=60,
            retry_seconds=1,
            worker_id=worker_id,
        )

    first = independent_processor("replica-one")
    second = independent_processor("replica-two")
    first_job_id = first.claim()

    assert first_job_id is not None
    assert second.claim() is None

    with original.factory() as session:
        completed = session.get(RoomTurnJob, first_job_id)
        assert completed is not None
        completed.status = "succeeded"
        completed.lease_owner = None
        completed.lease_expires_at = None
        completed.heartbeat_at = None
        completed.completed_at = now_utc()
        session.commit()

    second_job_id = second.claim()

    assert second_job_id is not None
    assert second_job_id != first_job_id


def test_room_turn_persists_trusted_authority_trace_and_stable_upstream_key(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    client.app.state.dnd_runtime.campaign_revision = 1
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={
            "Idempotency-Key": "authority-context-room-turn",
            "traceparent": traceparent,
            "baggage": "tenant=test",
        },
        json={"content": "I inspect the sealed gate.", "mode": "action", "base_revision": 1},
    )

    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["id"]
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, job_id)
        assert job is not None
        assert job.trace_context == {"traceparent": traceparent, "baggage": "tenant=test"}
        assert job.authority_context["schema"] == "sagasmith.auth-context/v2"
        assert job.authority_context["requester_principal"] == f"user:{owner['id']}"
        assert job.authority_context["room_turn_id"] == job_id
        assert job.authority_context["base_revision"] == 1
        assert job.authority_context["target_service"] == "sagasmith-dnd-mcp"
        assert job.authority_context["authorized_audience"] == "sagasmith-dnd-mcp"
        assert job.authority_context["allowed_operations"]
        assert job.authority_context["allowed_operations"] == sorted(
            job.authority_context["allowed_operations"]
        )
        assert len(job.authority_context["allowed_operations"]) <= 16
        assert "token" not in json.dumps(job.authority_context).lower()
    call = agent_runtime.calls[0]
    assert call["idempotency_key"] == f"room-turn:{job_id}"
    assert call["trace_context"]["traceparent"] == traceparent
    assert call["content"] == "I inspect the sealed gate."
    assert "I inspect the sealed gate." not in json.dumps(call["context"]["authority_context"])
    assert call["context"]["authority_context"]["room_turn_id"] == job_id
    run_id = call["context"]["run_id"]
    run_id_schema = call["context"]["response_contract"]["terminal"]["parameters"][
        "properties"
    ]["run_id"]
    assert run_id_schema["const"] == run_id_schema["default"] == run_id


def test_room_turn_rejects_stale_base_revision_with_recovery_details(
    client: TestClient,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "stale-room-turn"},
        json={"content": "I act on stale state.", "mode": "action", "base_revision": 6},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["retryable"] is True
    assert detail["base_revision"] == 6
    assert detail["current_revision"] == 1
    with client.app.state.session_factory() as session:
        assert (
            session.scalar(
                select(RoomTurnJob).where(RoomTurnJob.idempotency_key == "stale-room-turn")
            )
            is None
        )


def test_room_turn_rejects_revision_changed_during_unlocked_authority_prefetch(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    # The Web projection accepted revision 1, while the authoritative MCP has
    # advanced to the fixture's revision 7 before the Agent is invoked.
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "authoritative-stale-room-turn"},
        json={"content": "I act on changing state.", "mode": "action", "base_revision": 1},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["retryable"] is False
    assert "refresh the room panel" in detail["message"]
    assert not agent_runtime.calls


def test_room_turn_projects_standard_mcp_image_to_private_host_artifact(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "room-media-player@example.com", "Player")
    login(client, "room-owner@example.com")
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="
    )
    agent_runtime.mcp_results = (
        {
            "content": [
                {"type": "text", "text": "Rendered combat grid."},
                {
                    "type": "image",
                    "data": base64.b64encode(image).decode("ascii"),
                    "mimeType": "image/png",
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "memory://room-grid/notes",
                        "mimeType": "text/html",
                        "text": "<script>must-not-run-inline</script>",
                    },
                },
            ],
            "structuredContent": {"revision": 7},
            "isError": False,
        },
    )
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "standard-mcp-image"},
        json={"content": "Show the battle grid.", "mode": "action"},
    )

    assert response.status_code == 200, response.text
    media = response.json()["agent_message"]["structured_payload"]["media"]
    assert len(media) == 2
    assert media[0]["schema"] == "sagasmith.host-media/v1"
    assert media[0]["kind"] == "image"
    artifact = client.get(media[0]["url"])
    assert artifact.status_code == 200
    assert artifact.headers["content-type"].startswith("image/png")
    assert artifact.content == image
    embedded = client.get(media[1]["url"])
    assert embedded.status_code == 200
    assert embedded.headers["content-disposition"].startswith("attachment;")
    assert embedded.headers["x-content-type-options"] == "nosniff"
    assert embedded.content == b"<script>must-not-run-inline</script>"
    login(client, "room-media-player@example.com")
    group_artifact = client.get(media[0]["url"])
    assert group_artifact.status_code == 200
    assert group_artifact.content == image
    assert player["id"]
    register(client, "room-media-outsider@example.com", "Outsider")
    assert client.get(media[0]["url"]).status_code == 403
    login(client, "room-owner@example.com")
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, response.json()["job"]["id"])
        row = session.get(RoomMediaArtifact, media[0]["artifact_id"])
        assert job is not None and row is not None
        assert job.agent_result["mcp_results"] == list(agent_runtime.mcp_results)


def test_room_turn_reuses_agent_result_when_projection_retry_recovers(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    original = dnd_runtime.get_campaign
    calls = 0

    async def fail_once(**arguments: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("projection temporarily unavailable")
        return await original(**arguments)

    dnd_runtime.get_campaign = fail_once
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "projection-recovery"},
        json={"content": "Commit once and publish after recovery.", "mode": "action"},
    )

    assert response.status_code == 200, response.text
    assert calls == 3
    assert len(agent_runtime.calls) == 1
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, response.json()["job"]["id"])
        reservation = session.get(QuotaReservation, job.reservation_id) if job else None
        assert job is not None and reservation is not None
        assert job.status == "succeeded"
        assert job.attempt == 2
        assert job.agent_result["request_id"] == "agent-request-1"
        assert reservation.status == "settled"


def test_queued_room_turn_can_be_cancelled_without_agent_call(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    client.portal.call(client.app.state.room_turn_jobs.close)
    client.app.state.settings.room_turn_inline_wait_seconds = 0
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "cancel-queued-turn"},
        json={"content": "Cancel this action.", "mode": "action"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["job"]["status"] == "queued"
    job_id = response.json()["job"]["id"]

    cancelled = client.post(f"/api/campaigns/campaign-1/room/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["status"] == "cancelled"
    assert not agent_runtime.calls
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, job_id)
        trigger = session.get(CampaignMessage, job.trigger_message_id) if job else None
        events = session.scalars(
            select(CampaignRoomEvent).where(CampaignRoomEvent.event_type == "agent.cancelled")
        ).all()
        assert trigger is not None and trigger.status == "cancelled"
        assert len(events) == 1


def test_running_room_turn_cancel_is_published_without_an_assistant_message(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    entered_agent = threading.Event()
    release_agent = threading.Event()
    original_complete = agent_runtime.complete

    async def delayed_complete(**arguments: Any):
        entered_agent.set()
        while not release_agent.is_set():
            await asyncio.sleep(0.01)
        return await original_complete(**arguments)

    agent_runtime.complete = delayed_complete
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": "cancel-running-turn"},
            json={"content": "Cancel after dispatch.", "mode": "action"},
        )
        assert entered_agent.wait(timeout=5)
        with client.app.state.session_factory() as session:
            job = session.scalar(
                select(RoomTurnJob).where(RoomTurnJob.idempotency_key == "cancel-running-turn")
            )
            assert job is not None and job.status == "running"
            job_id = job.id
        cancelled = client.post(f"/api/campaigns/campaign-1/room/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["job"]["status"] == "running"
        release_agent.set()
        response = future.result(timeout=10)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cancelled"
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, job_id)
        assistants = session.scalars(
            select(CampaignMessage).where(
                CampaignMessage.trigger_message_id == job.trigger_message_id
            )
        ).all()
        assert job is not None and job.status == "cancelled"
        assert assistants == []


def test_agent_timeout_has_retryable_service_unavailable_contract(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    client.app.state.settings.room_turn_worker_max_attempts = 1

    async def timeout(**_arguments: Any):
        raise AgentRuntimeError("Agent completion timed out", retryable=True, code="agent_timeout")

    agent_runtime.complete = timeout
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "agent-timeout-turn"},
        json={"content": "Time out safely.", "mode": "action"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "agent_timeout",
        "retryable": True,
        "message": "Agent completion timed out",
        "recovery": "Retry with the same idempotency key.",
        "job_id": response.json()["detail"]["job_id"],
    }
    with client.app.state.session_factory() as session:
        assert session.scalars(
            select(CampaignRoomEvent).where(CampaignRoomEvent.event_type == "state.changed")
        ).all() == []
        assert session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "state.changed")
        ).all() == []


def test_expired_inflight_job_recovers_when_a_fresh_web_app_starts(
    tmp_path, dnd_runtime: FakeDndRuntime, agent_runtime: FakeAgentRuntime
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'restart-room.db').as_posix()}"
    settings = Settings(
        env="test",
        database_url=database_url,
        session_secret="restart-session-secret-at-least-thirty-two-characters",
        private_storage_dir=str(tmp_path / "private"),
        exchange_dir=str(tmp_path / "exchange"),
        public_origin="http://testserver",
        room_turn_inline_wait_seconds=0,
    )
    first_app = create_app(
        settings,
        make_engine(database_url),
        dnd_runtime,
        agent_runtime,
        coc_runtime=dnd_runtime,
        narrative_runtime=dnd_runtime,
    )
    with TestClient(first_app) as first:
        first.headers["Origin"] = "http://testserver"
        register(first, "room-owner@example.com", "DM")
        create_campaign(first)
        first.portal.call(first.app.state.room_turn_jobs.close)
        queued = first.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": "fresh-app-recovery"},
            json={"content": "Recover in another Web process.", "mode": "action"},
        )
        job_id = queued.json()["job"]["id"]
        with first.app.state.session_factory() as session:
            job = session.get(RoomTurnJob, job_id)
            assert job is not None
            job.status = "running"
            job.attempt = 1
            job.lease_owner = "terminated-web-process"
            job.lease_expires_at = now_utc() - timedelta(seconds=1)
            session.commit()

    second_app = create_app(
        settings,
        make_engine(database_url),
        dnd_runtime,
        agent_runtime,
        coc_runtime=dnd_runtime,
        narrative_runtime=dnd_runtime,
    )
    with TestClient(second_app) as second:
        second.headers["Origin"] = "http://testserver"
        login(second, "room-owner@example.com")
        deadline = time.monotonic() + 5
        status_value = "running"
        while time.monotonic() < deadline:
            status_value = second.get(
                f"/api/campaigns/campaign-1/room/jobs/{job_id}"
            ).json()["job"]["status"]
            if status_value == "succeeded":
                break
            time.sleep(0.02)
        assert status_value == "succeeded"
    assert len(agent_runtime.calls) == 1


def test_expired_worker_lease_recovers_and_completes_after_restart(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    processor = client.app.state.room_turn_jobs
    client.portal.call(processor.close)
    client.app.state.settings.room_turn_inline_wait_seconds = 0
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "lease-recovery-turn"},
        json={"content": "Recover this action.", "mode": "action"},
    )
    job_id = response.json()["job"]["id"]
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, job_id)
        assert job is not None
        job.status = "running"
        job.attempt = 1
        job.lease_owner = "dead-worker"
        job.lease_expires_at = now_utc() - timedelta(seconds=1)
        session.commit()

    assert processor.recover_expired() == 1
    with client.app.state.session_factory() as session:
        recovered = session.get(RoomTurnJob, job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_owner is None
    client.portal.call(processor.start)
    processor.notify()
    deadline = time.monotonic() + 5
    status_value = "queued"
    while time.monotonic() < deadline:
        status_value = client.get(f"/api/campaigns/campaign-1/room/jobs/{job_id}").json()["job"][
            "status"
        ]
        if status_value == "succeeded":
            break
        time.sleep(0.02)
    assert status_value == "succeeded"
    assert len(agent_runtime.calls) == 1


def test_expired_quota_reservation_remains_counted_while_job_is_active(
    client: TestClient,
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "quota-lease-turn"},
        json={"content": "Reserve quota durably.", "mode": "action"},
    )
    job_id = response.json()["job"]["id"]
    with client.app.state.session_factory() as session:
        job = session.get(RoomTurnJob, job_id)
        reservation = session.get(QuotaReservation, job.reservation_id) if job else None
        assert job is not None and reservation is not None
        job.status = "running"
        reservation.status = "reserved"
        reservation.expires_at = now_utc() - timedelta(seconds=1)
        session.commit()
        current = balance(session, owner["id"], "llm_tokens")
        assert current.reserved == reservation.reserved_quantity
        assert reservation.status == "reserved"
        job.status = "failed"
        session.commit()
        balance(session, owner["id"], "llm_tokens")
        assert reservation.status == "expired"


def test_async_room_action_releases_database_before_agent_and_mcp_awaits(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    captured_sessions: list[Any] = []
    observed_awaits: list[str] = []

    async def captured_async_db(request: Request):
        async with request.app.state.async_session_factory() as session:
            captured_sessions.append(session)
            try:
                yield session
            finally:
                if session.in_transaction():
                    await session.rollback()

    def assert_database_released(label: str) -> None:
        assert len(captured_sessions) == 1
        assert not captured_sessions[0].in_transaction()
        assert not client.app.state.room_turn_jobs._transaction_lock.locked()
        assert not any(
            lock.locked() for lock in client.app.state.room_turn_jobs._settlement_locks.values()
        )
        observed_awaits.append(label)

    original_agent_complete = agent_runtime.complete
    original_campaign_get = dnd_runtime.get_campaign
    original_resolution_get = dnd_runtime.get_resolution_presentation

    async def checked_agent_complete(**arguments: Any):
        assert_database_released("agent")
        return await original_agent_complete(**arguments)

    async def checked_campaign_get(**arguments: Any):
        assert_database_released("campaign")
        return await original_campaign_get(**arguments)

    async def checked_resolution_get(**arguments: Any):
        assert_database_released("projection")
        return await original_resolution_get(**arguments)

    agent_runtime.complete = checked_agent_complete
    dnd_runtime.get_campaign = checked_campaign_get
    dnd_runtime.get_resolution_presentation = checked_resolution_get
    dnd_runtime.resolution_presentations["transaction-boundary"] = {
        "schema": "sagasmith.resolution-presentation/v1",
        "system_id": "dnd5e",
        "thread_id": "transaction-boundary",
        "event_sequence": 1,
        "operation": "character.ability",
        "status": "settled",
        "audience": {"scope": "public", "actor_refs": [], "disclosure": "public"},
        "actor_refs": [],
        "rolls": [],
        "outcome": {"success": True},
        "pending_choice": None,
        "campaign_revision": 7,
        "random_stream_receipt": {"draw_count": 0},
    }

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "transaction-boundary",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {
                            "type": "resolution_ref",
                            "block_id": "transaction-boundary-resolution",
                            "resolution_id": "transaction-boundary",
                        }
                    ],
                }
            ],
            "suggestions": [],
        }

    agent_runtime.structured_output_factory = output
    client.app.dependency_overrides[get_async_db] = captured_async_db
    try:
        response = client.post(
            "/api/campaigns/campaign-1/room/messages",
            headers={"Idempotency-Key": "transaction-boundary-action"},
            json={"content": "Check transaction boundaries.", "mode": "action"},
        )
    finally:
        client.app.dependency_overrides.pop(get_async_db, None)

    assert response.status_code == 200, response.text
    assert observed_awaits == ["campaign", "agent", "campaign", "projection"]


def test_async_projection_refresh_releases_database_before_mcp_await(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "projection-owner@example.com", "Projection DM")
    create_campaign(client)
    captured_sessions: list[Any] = []

    async def captured_async_db(request: Request):
        async with request.app.state.async_session_factory() as session:
            captured_sessions.append(session)
            try:
                yield session
            finally:
                if session.in_transaction():
                    await session.rollback()

    original_panel_state = dnd_runtime.get_panel_state

    async def checked_panel_state(**arguments: Any):
        assert len(captured_sessions) == 1
        assert not captured_sessions[0].in_transaction()
        assert client.app.state.async_engine.sync_engine.pool.checkedout() == 0
        return await original_panel_state(**arguments)

    dnd_runtime.get_panel_state = checked_panel_state
    client.app.dependency_overrides[get_async_db] = captured_async_db
    try:
        response = client.get("/api/campaigns/campaign-1/room/panel")
    finally:
        client.app.dependency_overrides.pop(get_async_db, None)

    assert response.status_code == 200, response.text
    assert response.json()["membership"]["role"] == "owner"


def test_async_projection_refresh_releases_database_when_mcp_fails(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "projection-failure@example.com", "Projection failure DM")
    create_campaign(client)

    async def unavailable_panel_state(**_arguments: Any) -> dict[str, Any]:
        assert client.app.state.async_engine.sync_engine.pool.checkedout() == 0
        raise RuntimeError("projection unavailable")

    dnd_runtime.get_panel_state = unavailable_panel_state
    response = client.get("/api/campaigns/campaign-1/room/panel")

    assert response.status_code == 502
    assert response.json() == {"detail": "projection unavailable"}
    assert client.app.state.async_engine.sync_engine.pool.checkedout() == 0


def test_panel_revision_short_circuit_skips_binding_projection(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "projection-revision@example.com", "Projection revision DM")
    create_campaign(client)

    response = client.get("/api/campaigns/campaign-1/room/panel?known_revision=7")

    assert response.status_code == 200
    assert response.json() == {"not_modified": True, "revision": 7}
    call_name, arguments = dnd_runtime.calls[-1]
    assert call_name == "panel_state"
    assert arguments["campaign_id"] == "campaign-1"
    assert arguments["principal_id"].startswith("user:")
    assert arguments["known_revision"] == 7


def test_panel_projection_cache_is_revision_and_authorization_scoped(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
) -> None:
    register(client, "projection-cache@example.com", "Projection cache DM")
    create_campaign(client)

    first = client.get("/api/campaigns/campaign-1/room/panel")
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 7
    assert len([call for call in dnd_runtime.calls if call[0] == "panel_state"]) == 1

    cached = client.get("/api/campaigns/campaign-1/room/panel")
    assert cached.status_code == 200, cached.text
    assert cached.json() == first.json()
    unchanged = client.get("/api/campaigns/campaign-1/room/panel?known_revision=7")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json() == {"not_modified": True, "revision": 7}
    assert len([call for call in dnd_runtime.calls if call[0] == "panel_state"]) == 1

    with client.app.state.session_factory.begin() as session:
        membership = session.scalar(select(CampaignMembershipProjection))
        assert membership is not None
        membership.authorization_epoch += 1

    refreshed = client.get("/api/campaigns/campaign-1/room/panel")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"] == 7
    assert len([call for call in dnd_runtime.calls if call[0] == "panel_state"]) == 2


def test_invalid_async_room_action_rolls_back_staged_room_work(
    client: TestClient,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    heartbeat_sentinel = datetime(2000, 1, 1, tzinfo=UTC)
    with client.app.state.session_factory.begin() as session:
        active_session = session.scalar(select(UserSession))
        assert active_session is not None
        active_session.last_seen_at = heartbeat_sentinel
        active_session_id = active_session.id

    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "invalid-private-room-action"},
        json={
            "content": "This audience does not exist.",
            "mode": "action",
            "audience": "private",
            "audience_user_ids": ["missing-user"],
        },
    )

    assert response.status_code == 422
    with client.app.state.session_factory() as session:
        message = session.scalar(
            select(CampaignMessage).where(
                CampaignMessage.client_message_id == "invalid-private-room-action"
            )
        )
        refreshed_session = session.get(UserSession, active_session_id)
    assert message is None
    assert refreshed_session is not None
    assert refreshed_session.last_seen_at > heartbeat_sentinel.replace(tzinfo=None)


def test_room_activity_accepts_only_safe_scoped_state_transitions(
    client: TestClient,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    posted = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "activity-trigger"},
        json={"content": "我检查现场。", "mode": "action"},
    )
    assert posted.status_code == 200, posted.text
    with client.app.state.session_factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.campaign_id == "campaign-1"))
        assert run is not None
        run.status = "running"
        run_id = run.id
        session.commit()
    token = _activity_token(
        client.app.state.settings.session_secret,
        "campaign-1",
        run_id,
    )
    endpoint = f"/api/campaigns/campaign-1/room/internal-activity/{run_id}"
    headers = {"Authorization": f"Bearer {token}"}
    started = client.post(
        endpoint,
        headers=headers,
        json={
            "schema": "sagasmith.room-activity/v1",
            "run_id": run_id,
            "activity_id": "rules-1",
            "audience": {"kind": "public"},
            "code": "reviewing_rules",
            "state": "started",
        },
    )
    assert started.status_code == 200, started.text
    completed = client.post(
        endpoint,
        headers=headers,
        json={
            "schema": "sagasmith.room-activity/v1",
            "run_id": run_id,
            "activity_id": "rules-1",
            "audience": {"kind": "public"},
            "code": "reviewing_rules",
            "state": "completed",
        },
    )
    assert completed.status_code == 200, completed.text
    leaked_roll = client.post(
        endpoint,
        headers=headers,
        json={
            "schema": "sagasmith.room-activity/v1",
            "run_id": run_id,
            "activity_id": "secret-roll",
            "audience": {"kind": "public"},
            "code": "resolving_roll",
            "state": "started",
        },
    )
    assert leaked_roll.status_code == 422
    with client.app.state.session_factory() as session:
        activity_events = session.scalars(
            select(CampaignRoomEvent).where(CampaignRoomEvent.event_type == "room.activity")
        ).all()
    assert [event.payload["state"] for event in activity_events] == [
        "started",
        "completed",
    ]
    assert all("text" not in event.payload for event in activity_events)


def test_room_audience_and_panel_actions_are_authorized_and_refreshable(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "room-private@example.com", "Player")

    private = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "private-message-1"},
        json={
            "content": "只告诉 DM。",
            "mode": "chat",
            "audience": "dm",
        },
    )
    assert private.status_code == 200
    panel = client.get("/api/campaigns/campaign-1/room/panel")
    assert panel.status_code == 200
    assert panel.json()["phase"] == "play"
    assert panel.json()["characters"][0]["name"] == "Aria"
    assert "combat_grid_templates" not in json.dumps(panel.json()["current_module"])

    forbidden = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "player-phase-change"},
        json={"action": "phase.set", "payload": {"phase": "lobby"}},
    )
    assert forbidden.status_code == 403

    login(client, "room-owner@example.com")
    dm_messages = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert any(item["content"] == "只告诉 DM。" for item in dm_messages)
    dm_panel = client.get("/api/campaigns/campaign-1/room/panel").json()
    assert (
        dm_panel["current_module"]["scene"]["profile_data"]["combat_grid_templates"][0]["id"]
        == "gate-ambush"
    )
    changed = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-phase-change"},
        json={"action": "phase.set", "payload": {"phase": "lobby"}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["message"]["message_type"] == "status"
    phase_call = next(item for item in dnd_runtime.calls if item[0] == "phase_set")
    assert phase_call[1]["principal_id"].startswith("user:")
    assert phase_call[1]["expected_revision"] == 7

    started = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-combat-start"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "positioning_mode": "agent",
                "name": "Gate Ambush",
            },
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["message"]["message_type"] == "combat"
    repeated = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-combat-start"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "positioning_mode": "agent",
                "name": "Gate Ambush",
            },
        },
    )
    assert repeated.status_code == 200
    assert len([item for item in dnd_runtime.calls if item[0] == "combat_start"]) == 1
    grid_started = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-grid-combat-start"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "participant_config": [{"actor_id": "actor-1", "position": {"x": 2, "y": 3}}],
                "positioning_mode": "grid",
                "name": "Grid Ambush",
                "battle_map": {
                    "width_cells": 20,
                    "height_cells": 14,
                    "blocked_cells": [],
                    "difficult_cells": [],
                },
                "battle_map_override_reason": "DM-created temporary grid",
            },
        },
    )
    assert grid_started.status_code == 200, grid_started.text
    grid_call = [item for item in dnd_runtime.calls if item[0] == "combat_start"][-1][1]
    assert grid_call["positioning_mode"] == "grid"
    assert grid_call["participant_config"][0]["position"] == {"x": 2, "y": 3}
    assert grid_call["battle_map"]["width_cells"] == 20
    template_started = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-template-combat-start"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "participant_config": [{"actor_id": "actor-1", "position": {"x": 4, "y": 2}}],
                "positioning_mode": "grid",
                "name": "Template Ambush",
                "battle_map_template_id": "gate-ambush",
            },
        },
    )
    assert template_started.status_code == 200, template_started.text
    template_call = [item for item in dnd_runtime.calls if item[0] == "combat_start"][-1][1]
    assert template_call["battle_map_template_id"] == "gate-ambush"
    assert template_call["battle_map"] is None

    map_authority_conflict = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-conflicting-map-authority"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "participant_config": [{"actor_id": "actor-1", "position": {"x": 0, "y": 0}}],
                "positioning_mode": "grid",
                "battle_map_template_id": "gate-ambush",
                "battle_map": {"width_cells": 6, "height_cells": 4},
            },
        },
    )
    assert map_authority_conflict.status_code == 422
    assert "mutually exclusive" in map_authority_conflict.text

    missing_map_authority = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "dm-missing-map-authority"},
        json={
            "action": "combat.start",
            "payload": {
                "participant_ids": ["actor-1"],
                "participant_config": [{"actor_id": "actor-1", "position": {"x": 0, "y": 0}}],
                "positioning_mode": "grid",
            },
        },
    )
    assert missing_map_authority.status_code == 422
    assert "one map authority" in missing_map_authority.text
    with client.app.state.session_factory() as session:
        event_types = session.scalars(
            select(CampaignRoomEvent.event_type).order_by(CampaignRoomEvent.sequence)
        ).all()
        projection_events = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "state.changed")
        ).all()
    assert event_types.count("state.changed") == 4
    assert len(projection_events) == 4
    for event_row in projection_events:
        assert isinstance(event_row.payload["authority_revision"], int)
        assert event_row.payload["changed_scopes"]
        assert event_row.payload["entity_ids"] == ["campaign-1"]
        assert event_row.payload["audience"] == {"kind": "public", "user_ids": []}

    login(client, "room-private@example.com")
    assert (
        client.put(
            "/api/campaigns/campaign-1/room/read", json={"last_read_sequence": 3}
        ).status_code
        == 200
    )
    mismatch = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "private-message-1"},
        json={"content": "不同内容", "mode": "chat"},
    )
    assert mismatch.status_code == 409
    changed_mode = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "private-message-1"},
        json={"content": "只告诉 DM。", "mode": "action", "audience": "dm"},
    )
    assert changed_mode.status_code == 409
    assert player["id"]


def test_noop_authoritative_receipt_does_not_emit_projection_invalidation(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    assert client.get("/api/campaigns/campaign-1/room/panel").status_code == 200

    async def unchanged_phase(**arguments: Any) -> dict[str, Any]:
        return {
            "result": {
                "effective_game_phase": arguments["tool_profile"],
                "campaign_revision": arguments["expected_revision"],
            }
        }

    dnd_runtime.set_game_phase = unchanged_phase
    response = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "noop-phase-change"},
        json={"action": "phase.set", "payload": {"phase": "play"}, "base_revision": 7},
    )
    assert response.status_code == 200
    with client.app.state.session_factory() as session:
        assert session.scalars(
            select(CampaignRoomEvent).where(CampaignRoomEvent.event_type == "state.changed")
        ).all() == []
        assert session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "state.changed")
        ).all() == []


def test_all_game_panel_intents_use_the_room_agent_and_emit_refresh(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)

    actions = (
        ("character.intent", {"actor_id": "actor-1", "intent": "进行察觉检定"}),
        ("play.intent", {"intent": "推进到门后的场景"}),
        ("combat.intent", {"actor_id": "actor-1", "intent": "攻击最近的敌人"}),
    )
    for index, (action, payload) in enumerate(actions):
        response = client.post(
            "/api/campaigns/campaign-1/room/panel/actions",
            headers={"Idempotency-Key": f"panel-intent-{index}"},
            json={"action": action, "payload": payload},
        )
        assert response.status_code == 200, response.text
        assert response.json()["agent_message"]["content"] == agent_runtime.content

    assert len(agent_runtime.calls) == 3
    assert agent_runtime.calls[0]["content"] == "角色 actor-1：进行察觉检定"
    with client.app.state.session_factory() as session:
        event_types = session.scalars(
            select(CampaignRoomEvent.event_type).order_by(CampaignRoomEvent.sequence)
        ).all()
    # The first intent refreshes the Web projection from revision 1 to the
    # authority's revision 7. Subsequent no-op receipts must not fan out cache
    # invalidations merely because another Agent turn completed.
    assert event_types.count("state.changed") == 1


def test_character_card_requires_private_actor_scope(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)

    dm_view = client.get("/api/campaigns/campaign-1/room/characters/actor-1")
    assert dm_view.status_code == 200
    assert dm_view.json()["actor"]["derived"]["armor_class"] == 16
    assert dm_view.json()["permissions"] == {
        "can_control": True,
        "can_view_private": True,
    }

    player = add_player(client, "room-card-player@example.com", "Player")
    forbidden = client.get("/api/campaigns/campaign-1/room/characters/actor-1")
    assert forbidden.status_code == 403

    login(client, "room-owner@example.com")
    granted = client.put(
        "/api/campaigns/campaign-1/actors/actor-1/binding",
        json={"user_id": player["id"], "can_control": True, "can_view_private": True},
    )
    assert granted.status_code == 200, granted.text
    login(client, "room-card-player@example.com")
    player_view = client.get("/api/campaigns/campaign-1/room/characters/actor-1")
    assert player_view.status_code == 200
    assert player_view.json()["actor"]["name"] == "Aria"
    card_call = [item for item in dnd_runtime.calls if item[0] == "character_card"][-1]
    assert card_call[1]["principal_id"] == f"user:{player['id']}"


def test_product_shell_contains_live_room_and_operable_panels(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'rel="manifest" href="/manifest.webmanifest"' in page.text
    for element_id in (
        'id="messages"',
        'id="character-sidebar"',
        'id="character-page-character"',
        'id="character-page-spells"',
        'id="character-page-inventory"',
        'id="character-page-party"',
        'id="action-context"',
        'id="play-panel"',
        'id="combat-panel"',
        'id="module-panel"',
    ):
        assert element_id in page.text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["icons"][0]["src"] == "/sagasmith-icon.svg"
    service_worker = client.get("/service-worker.js")
    assert service_worker.status_code == 200
    assert 'url.pathname.startsWith("/api/")' in service_worker.text
    assert client.get("/sagasmith-icon.svg").status_code == 200


def test_structured_room_turn_projects_actor_identity_and_personal_suggestions(
    client: TestClient, agent_runtime: FakeAgentRuntime, dnd_runtime: FakeDndRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    agent_runtime.tool_receipts = (
        {
            "tool": "mcp_sagasmith_dnd_character_check",
            "auth_context_receipt": {
                "schema": "sagasmith.auth-context/v1",
                "actor_principal": f"user:{owner['id']}",
                "conversation_principal": "session:campaign-1:room:owner",
                "tenant_id": "",
                "campaign_id": "campaign-1",
                "session_id": "campaign-1:room:owner",
                "tool": "character_check",
                "authorization_epoch": 3,
                "revision": 7,
                "nonce": "room-receipt-nonce",
            },
            "structured_content": {"result": {"resolution_id": "resolution-1", "total": 17}},
        },
    )
    dnd_runtime.resolution_presentations["resolution-1"] = {
        "schema": "sagasmith.resolution-presentation/v1",
        "system_id": "dnd5e",
        "thread_id": "resolution-1",
        "event_sequence": 1,
        "operation": "character.ability",
        "status": "settled",
        "audience": {"scope": "public", "actor_refs": [], "disclosure": "public"},
        "actor_refs": [],
        "rolls": [
            {
                "roll_id": "resolution-1:roll:1",
                "expression": "d20",
                "dice": [17],
                "kept": [17],
                "modifier": 0,
                "total": 17,
            }
        ],
        "outcome": {"success": True},
        "pending_choice": None,
        "campaign_revision": 7,
        "random_stream_receipt": {"draw_count": 1},
    }

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "public-main",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {
                            "type": "narration",
                            "block_id": "n1",
                            "text": "门后的脚步声停了。",
                        },
                        {
                            "type": "performance",
                            "block_id": "p1",
                            "speaker": {
                                "kind": "published_actor",
                                "label": "守门人",
                                "actor_ref": "secret-npc-id",
                            },
                            "beats": [
                                {"type": "action", "text": "他把灯抬高了一些。"},
                                {"type": "speech", "text": "报上姓名。"},
                            ],
                            "provenance": {"kind": "agent_ruling"},
                        },
                        {
                            "type": "resolution_ref",
                            "block_id": "r1",
                            "resolution_id": "resolution-1",
                        },
                        {"type": "prompt", "block_id": "q1", "text": "你怎么回应？"},
                    ],
                }
            ],
            "suggestions": [{"id": "s1", "text": "我出示通行文书。"}],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "structured-room-turn-1"},
        json={"content": "我敲门。", "mode": "action"},
    )
    assert response.status_code == 200, response.text
    message = response.json()["agent_message"]
    assert message["message_type"] == "presentation"
    assert "secret-npc-id" not in str(message["structured_payload"])
    performance = message["structured_payload"]["blocks"][1]
    assert performance["speaker"]["publication_ref"].startswith("actor-pub-")
    assert message["structured_payload"]["blocks"][2]["verified"] is True
    assert message["structured_payload"]["suggestions"][0]["text"] == "我出示通行文书。"
    assert "target_user_id" not in message["structured_payload"]["suggestions"][0]
    with client.app.state.session_factory() as session:
        suggestion = session.scalar(
            select(CampaignSuggestion).where(
                CampaignSuggestion.message_id == message["id"],
                CampaignSuggestion.suggestion_id == "s1",
            )
        )
        assert suggestion is not None
        assert suggestion.room_id == response.json()["message"]["room_id"]
        assert suggestion.target_user_id == owner["id"]
        assert suggestion.expired is False
        assert suggestion.valid_revision == 7
        assert suggestion.valid_phase == "play"

    player = add_player(client, "room-structured-player@example.com", "Player")
    timeline = client.get("/api/campaigns/campaign-1/room/messages").json()
    public_agent = next(item for item in timeline if item["sender_type"] == "agent")
    assert public_agent["structured_payload"]["suggestions"] == []
    assert player["id"] != owner["id"]
    with client.app.state.session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "campaign.room.agent.complete",
                AuditEvent.subject_id == response.json()["message"]["id"],
            )
        )
        assert audit is not None
        assert audit.details["auth_context_receipts"][0]["actor_principal"] == (
            f"user:{owner['id']}"
        )
        assert audit.details["auth_context_receipts"][0]["conversation_principal"] == (
            "session:campaign-1:room:owner"
        )


def test_public_room_turn_cannot_reference_dm_only_resolution(
    client: TestClient, agent_runtime: FakeAgentRuntime, dnd_runtime: FakeDndRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "hidden-roll-player@example.com", "Player")
    login(client, "room-owner@example.com")
    resolution_id = "dm-hidden-resolution"
    dnd_runtime.resolution_presentations[resolution_id] = {
        "schema": "sagasmith.resolution-presentation/v1",
        "system_id": "dnd5e",
        "thread_id": resolution_id,
        "event_sequence": 1,
        "operation": "dice.roll",
        "status": "settled",
        "audience": {"scope": "dm", "actor_refs": [], "disclosure": "hidden"},
        "actor_refs": [],
        "rolls": [{"roll_id": "secret-roll", "dice": [19], "total": 19}],
        "outcome": {"total": 19},
        "pending_choice": None,
        "campaign_revision": 7,
    }
    dnd_runtime.resolution_denied_principals.add(f"user:{player['id']}")

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "unsafe-public-roll",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {
                            "type": "resolution_ref",
                            "block_id": "r1",
                            "resolution_id": resolution_id,
                        }
                    ],
                }
            ],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "reject-hidden-roll-publication"},
        json={"content": "我环顾四周。", "mode": "action"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mcp_projection_invalid"
    assert response.json()["detail"]["retryable"] is False
    timeline = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert all(
        resolution_id not in str(message.get("structured_payload") or {}) for message in timeline
    )


def test_room_turn_globally_bounds_resolution_actor_and_suggestion_projections(
    client: TestClient, agent_runtime: FakeAgentRuntime, dnd_runtime: FakeDndRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    player = add_player(client, "projection-player@example.com", "Player")
    login(client, "room-owner@example.com")
    for actor_id in ("projection-actor-1", "projection-actor-2"):
        bound = client.put(
            f"/api/campaigns/campaign-1/actors/{actor_id}/binding",
            json={"user_id": player["id"], "can_control": True, "can_view_private": True},
        )
        assert bound.status_code == 200, bound.text
    login(client, "projection-player@example.com")
    resolution_ids = [f"parallel-resolution-{index}" for index in range(9)]
    for resolution_id in resolution_ids:
        dnd_runtime.resolution_presentations[resolution_id] = {
            "schema": "sagasmith.resolution-presentation/v1",
            "system_id": "dnd5e",
            "thread_id": resolution_id,
            "event_sequence": 1,
            "operation": "dice.roll",
            "status": "settled",
            "audience": {"scope": "public", "actor_refs": [], "disclosure": "public"},
            "actor_refs": [],
            "rolls": [],
            "outcome": {"success": True},
            "pending_choice": None,
            "campaign_revision": 7,
        }

    active = 0
    maximum = 0
    starts: list[tuple[str, str]] = []
    original_resolution = dnd_runtime.get_resolution_presentation
    original_actor = dnd_runtime.get_character_card

    async def delayed_projection(**arguments: Any) -> dict[str, Any]:
        nonlocal active, maximum
        starts.append(("resolution", arguments["resolution_id"]))
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return await original_resolution(**arguments)
        finally:
            active -= 1

    async def delayed_actor(**arguments: Any) -> dict[str, Any]:
        nonlocal active, maximum
        starts.append(("actor", arguments["character_id"]))
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return await original_actor(**arguments)
        finally:
            active -= 1

    dnd_runtime.get_resolution_presentation = delayed_projection
    dnd_runtime.get_character_card = delayed_actor

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "parallel-public-rolls",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {"type": "narration", "block_id": "n1", "text": "骰声接连落定。"},
                        *[
                            {
                                "type": "performance",
                                "block_id": f"p{index}",
                                "speaker": {
                                    "kind": "published_actor",
                                    "label": "untrusted",
                                    "actor_ref": actor_id,
                                },
                                "beats": [{"type": "action", "text": "他按计划行动。"}],
                                "provenance": {
                                    "kind": "player_intent",
                                    "source_message_id": context["trigger_message_id"],
                                },
                            }
                            for index, actor_id in enumerate(
                                ("projection-actor-1", "projection-actor-2"),
                                start=1,
                            )
                        ],
                        *[
                            {
                                "type": "resolution_ref",
                                "block_id": f"r{index}",
                                "resolution_id": resolution_id,
                            }
                            for index, resolution_id in enumerate(resolution_ids)
                        ],
                    ],
                }
            ],
            "suggestions": [
                {
                    "id": "projection-suggestion",
                    "text": "继续执行计划。",
                    "actor_ref": "projection-actor-1",
                }
            ],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "parallel-resolution-projection"},
        json={"content": "依次检定。", "mode": "action"},
    )

    assert response.status_code == 200, response.text
    assert maximum == 16
    assert {kind for kind, _ in starts[:16]} == {"resolution", "actor"}
    last_resolution = max(index for index, (kind, _) in enumerate(starts) if kind == "resolution")
    assert any(
        kind == "actor" and actor_id == "projection-actor-1"
        for kind, actor_id in starts[:last_resolution]
    )
    assert starts.count(("actor", "projection-actor-1")) == 3
    blocks = response.json()["agent_message"]["structured_payload"]["blocks"]
    assert [block["resolution_id"] for block in blocks[3:]] == resolution_ids
    assert (
        response.json()["agent_message"]["structured_payload"]["suggestions"][0]["actor_revision"]
        == 3
    )
    assert owner["id"] != player["id"]


def test_room_projection_queries_do_not_scale_with_output_actor_audience_product(
    client: TestClient, agent_runtime: FakeAgentRuntime, dnd_runtime: FakeDndRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    add_player(client, "query-player@example.com", "Player")
    login(client, "room-owner@example.com")
    actor_ids = [f"query-actor-{index}" for index in range(4)]
    for actor_id in actor_ids:
        bound = client.put(
            f"/api/campaigns/campaign-1/actors/{actor_id}/binding",
            json={"user_id": owner["id"], "can_control": True, "can_view_private": True},
        )
        assert bound.status_code == 200, bound.text
    resolution_ids = [f"query-resolution-{index}" for index in range(4)]
    for resolution_id in resolution_ids:
        dnd_runtime.resolution_presentations[resolution_id] = {
            "schema": "sagasmith.resolution-presentation/v1",
            "system_id": "dnd5e",
            "thread_id": resolution_id,
            "event_sequence": 1,
            "operation": "dice.roll",
            "status": "settled",
            "audience": {"scope": "public", "actor_refs": [], "disclosure": "public"},
            "actor_refs": [],
            "rolls": [],
            "outcome": {"success": True},
            "pending_choice": None,
            "campaign_revision": 7,
        }

    def messages(
        context: dict[str, Any],
        *,
        output_count: int,
        actors_per_output: int,
        resolutions_per_output: int,
    ) -> list[dict[str, Any]]:
        audiences = (
            {"kind": "public"},
            {"kind": "dm"},
            {"kind": "actors", "actor_refs": [actor_ids[0]]},
            {"kind": "public"},
        )
        return [
            {
                "output_id": f"query-output-{output_index}",
                "audience": audiences[output_index],
                "blocks": [
                    *[
                        {
                            "type": "performance",
                            "block_id": f"p-{output_index}-{actor_index}",
                            "speaker": {
                                "kind": "published_actor",
                                "label": "untrusted",
                                "actor_ref": actor_ids[actor_index],
                            },
                            "beats": [{"type": "action", "text": "按玩家意图行动。"}],
                            "provenance": {
                                "kind": "player_intent",
                                "source_message_id": context["trigger_message_id"],
                            },
                        }
                        for actor_index in range(actors_per_output)
                    ],
                    *[
                        {
                            "type": "resolution_ref",
                            "block_id": f"r-{output_index}-{resolution_index}",
                            "resolution_id": resolution_ids[resolution_index],
                        }
                        for resolution_index in range(resolutions_per_output)
                    ],
                ],
            }
            for output_index in range(output_count)
        ]

    def run_and_count(
        *,
        idempotency_key: str,
        output_count: int,
        actors_per_output: int,
        resolutions_per_output: int,
    ) -> tuple[dict[str, int], int]:
        def output(context: dict[str, Any]) -> dict[str, Any]:
            return {
                "schema": "sagasmith.room-turn/v1",
                "run_id": context["run_id"],
                "messages": messages(
                    context,
                    output_count=output_count,
                    actors_per_output=actors_per_output,
                    resolutions_per_output=resolutions_per_output,
                ),
                "suggestions": [
                    {
                        "id": "query-suggestion",
                        "text": "继续。",
                        "actor_ref": actor_ids[0],
                    }
                ],
            }

        agent_runtime.structured_output_factory = output
        statements: list[str] = []

        def collect(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement.lower())

        call_start = len(dnd_runtime.calls)
        observed_engines = (
            client.app.state.engine,
            client.app.state.async_engine.sync_engine,
        )
        for observed_engine in observed_engines:
            event.listen(observed_engine, "before_cursor_execute", collect)
        try:
            response = client.post(
                "/api/campaigns/campaign-1/room/messages",
                headers={"Idempotency-Key": idempotency_key},
                json={"content": idempotency_key, "mode": "action"},
            )
        finally:
            for observed_engine in observed_engines:
                event.remove(observed_engine, "before_cursor_execute", collect)
        assert response.status_code == 200, response.text
        tables = (
            "campaign_projections",
            "campaign_membership_projections",
            "users",
            "actor_binding_projections",
        )
        counts = {
            table: sum(
                statement.lstrip().startswith("select") and table in statement
                for statement in statements
            )
            for table in tables
        }
        projection_calls = sum(
            name in {"resolution_presentation", "character_card"}
            for name, _ in dnd_runtime.calls[call_start:]
        )
        return counts, projection_calls

    small_counts, small_projection_calls = run_and_count(
        idempotency_key="projection-query-small",
        output_count=1,
        actors_per_output=1,
        resolutions_per_output=1,
    )
    large_counts, large_projection_calls = run_and_count(
        idempotency_key="projection-query-large",
        output_count=4,
        actors_per_output=4,
        resolutions_per_output=4,
    )

    assert large_projection_calls > small_projection_calls
    for counts in (small_counts, large_counts):
        assert counts["campaign_projections"] <= 6
        assert counts["campaign_membership_projections"] <= 6
        assert counts["users"] <= 5
        assert counts["actor_binding_projections"] <= 2


def test_suggestion_cannot_reference_hidden_or_stale_pending_choice(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "ordinary-output",
                    "audience": {"kind": "public"},
                    "blocks": [{"type": "prompt", "block_id": "p1", "text": "你要怎么做？"}],
                }
            ],
            "suggestions": [
                {
                    "id": "hidden-choice",
                    "text": "使用那个隐藏选项",
                    "pending_choice_id": "dm-only-choice",
                }
            ],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "reject-hidden-suggestion"},
        json={"content": "我等待。", "mode": "action"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mcp_projection_invalid"
    assert response.json()["detail"]["retryable"] is False


def test_structured_room_turn_rejects_agent_ruling_for_human_pc(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    bound = client.put(
        "/api/campaigns/campaign-1/actors/actor-1/binding",
        json={"user_id": owner["id"], "can_control": True, "can_view_private": True},
    )
    assert bound.status_code == 200, bound.text

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "invalid-pc",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {
                            "type": "performance",
                            "block_id": "p1",
                            "speaker": {
                                "kind": "published_actor",
                                "label": "Aria",
                                "actor_ref": "actor-1",
                            },
                            "beats": [{"type": "speech", "text": "我投降。"}],
                            "provenance": {"kind": "agent_ruling"},
                        }
                    ],
                }
            ],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "invalid-pc-ruling-1"},
        json={"content": "我观察敌人。", "mode": "action"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mcp_projection_invalid"
    assert response.json()["detail"]["retryable"] is False


def test_structured_room_turn_accepts_pc_performance_from_exact_trigger_intent(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    assert (
        client.put(
            "/api/campaigns/campaign-1/actors/actor-1/binding",
            json={"user_id": owner["id"], "can_control": True, "can_view_private": True},
        ).status_code
        == 200
    )

    def output(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "pc-intent",
                    "audience": {"kind": "public"},
                    "blocks": [
                        {
                            "type": "performance",
                            "block_id": "p1",
                            "speaker": {
                                "kind": "published_actor",
                                "label": "Aria",
                                "actor_ref": "actor-1",
                            },
                            "beats": [{"type": "action", "text": "她抬手敲了三下门。"}],
                            "provenance": {
                                "kind": "player_intent",
                                "source_message_id": context["trigger_message_id"],
                            },
                        }
                    ],
                }
            ],
        }

    agent_runtime.structured_output_factory = output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "valid-pc-intent-1"},
        json={"content": "我抬手敲三下门。", "mode": "action"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["agent_message"]["content"] == "Aria：她抬手敲了三下门。"
