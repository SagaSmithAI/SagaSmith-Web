from typing import Any

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi.testclient import TestClient
from sqlalchemy import select

from sagasmith_service.models import CampaignRoomEvent

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
