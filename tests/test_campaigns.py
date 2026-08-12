from typing import Any

from conftest import FakeDndRuntime
from fastapi.testclient import TestClient


def register(client: TestClient, email: str, name: str) -> dict[str, Any]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple", "display_name": name},
    )
    assert response.status_code == 201
    return response.json()["user"]


def login(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200


def test_campaign_join_and_actor_binding_use_authoritative_runtime(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    owner = register(client, "dm@example.com", "DM")
    created = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "create-campaign-001"},
        json={"name": "龙枪纪元", "edition": "2024"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["id"] == "campaign-1"
    assert dnd_runtime.calls[0][1]["principal_id"] == f"user:{owner['id']}"
    runtime = client.get("/api/campaigns/campaign-1/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["result"]["effective_game_phase"] == "play"
    runtime_call = next(call for call in dnd_runtime.calls if call[0] == "campaign_get")
    assert runtime_call[1]["principal_id"] == f"user:{owner['id']}"

    player = register(client, "player@example.com", "Player")
    requested = client.post(
        "/api/campaigns/campaign-1/join-requests", json={"message": "申请加入"}
    )
    assert requested.status_code == 201

    login(client, "dm@example.com")
    decided = client.post(
        f"/api/campaigns/campaign-1/join-requests/{requested.json()['id']}/decision",
        json={"decision": "approved"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    grant = next(call for call in dnd_runtime.calls if call[0] == "campaign_access")
    assert grant[1]["principal_id"] == f"user:{player['id']}"
    assert grant[1]["by_principal_id"] == f"user:{owner['id']}"

    bound = client.put(
        "/api/campaigns/campaign-1/actors/fighter-1/binding",
        json={"user_id": player["id"], "can_control": True, "can_view_private": True},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["actor_id"] == "fighter-1"
    assert any(call[0] == "actor_access" for call in dnd_runtime.calls)

    login(client, "player@example.com")
    listed = client.get("/api/campaigns")
    assert [campaign["id"] for campaign in listed.json()] == ["campaign-1"]
    assert client.get("/api/campaigns/campaign-1/join-requests").status_code == 403


def test_runtime_failure_does_not_approve_join(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    register(client, "dm2@example.com", "DM")
    client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "create-campaign-002"},
        json={"name": "故障测试"},
    )
    register(client, "player2@example.com", "Player")
    requested = client.post("/api/campaigns/campaign-1/join-requests", json={})
    login(client, "dm2@example.com")
    dnd_runtime.fail_grant = True
    failed = client.post(
        f"/api/campaigns/campaign-1/join-requests/{requested.json()['id']}/decision",
        json={"decision": "approved"},
    )
    assert failed.status_code == 502
    pending = client.get("/api/campaigns/campaign-1/join-requests")
    assert pending.json()[0]["status"] == "pending"


def test_campaign_creation_is_idempotent(client: TestClient, dnd_runtime: FakeDndRuntime) -> None:
    register(client, "idempotent@example.com", "DM")
    headers = {"Idempotency-Key": "same-create-key"}
    first = client.post("/api/campaigns", headers=headers, json={"name": "唯一战役"})
    second = client.post("/api/campaigns", headers=headers, json={"name": "唯一战役"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert len([call for call in dnd_runtime.calls if call[0] == "campaign_create"]) == 1
