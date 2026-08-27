import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from sagasmith_service.api.rooms import _activity_token
from sagasmith_service.models import (
    AgentRun,
    AuditEvent,
    CampaignMessage,
    CampaignRoomEvent,
    CampaignSuggestion,
)

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


def test_invalid_async_room_action_rolls_back_staged_room_work(
    client: TestClient,
) -> None:
    register(client, "room-owner@example.com", "DM")
    create_campaign(client)

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
    assert message is None


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

    forbidden = client.post(
        "/api/campaigns/campaign-1/room/panel/actions",
        headers={"Idempotency-Key": "player-phase-change"},
        json={"action": "phase.set", "payload": {"phase": "lobby"}},
    )
    assert forbidden.status_code == 403

    login(client, "room-owner@example.com")
    dm_messages = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert any(item["content"] == "只告诉 DM。" for item in dm_messages)
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
                "participant_config": [
                    {"actor_id": "actor-1", "position": {"x": 2, "y": 3}}
                ],
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
    with client.app.state.session_factory() as session:
        event_types = session.scalars(
            select(CampaignRoomEvent.event_type).order_by(CampaignRoomEvent.sequence)
        ).all()
    assert event_types.count("state.changed") == 3

    login(client, "room-private@example.com")
    assert client.put(
        "/api/campaigns/campaign-1/room/read", json={"last_read_sequence": 3}
    ).status_code == 200
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
    assert event_types.count("state.changed") == 3


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
            "structured_content": {
                "result": {"resolution_id": "resolution-1", "total": 17}
            },
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
    assert response.status_code == 502
    assert response.json()["detail"] == "Agent returned an invalid structured room response"
    timeline = client.get("/api/campaigns/campaign-1/room/messages").json()
    assert all(
        resolution_id not in str(message.get("structured_payload") or {})
        for message in timeline
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
    last_resolution = max(
        index for index, (kind, _) in enumerate(starts) if kind == "resolution"
    )
    assert any(
        kind == "actor" and actor_id == "projection-actor-1"
        for kind, actor_id in starts[:last_resolution]
    )
    assert starts.count(("actor", "projection-actor-1")) == 3
    blocks = response.json()["agent_message"]["structured_payload"]["blocks"]
    assert [block["resolution_id"] for block in blocks[3:]] == resolution_ids
    assert response.json()["agent_message"]["structured_payload"]["suggestions"][0][
        "actor_revision"
    ] == 3
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
    assert large_counts == small_counts
    assert large_counts == {
        "campaign_projections": 1,
        "campaign_membership_projections": 2,
        "users": 2,
        "actor_binding_projections": 1,
    }


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
                    "blocks": [
                        {"type": "prompt", "block_id": "p1", "text": "你要怎么做？"}
                    ],
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
    assert response.status_code == 502
    assert response.json()["detail"] == "Agent returned an invalid structured room response"


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
    assert response.status_code == 502
    assert response.json()["detail"] == "Agent returned an invalid structured room response"


def test_structured_room_turn_accepts_pc_performance_from_exact_trigger_intent(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    owner = register(client, "room-owner@example.com", "DM")
    create_campaign(client)
    assert client.put(
        "/api/campaigns/campaign-1/actors/actor-1/binding",
        json={"user_id": owner["id"], "can_control": True, "can_view_private": True},
    ).status_code == 200

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
