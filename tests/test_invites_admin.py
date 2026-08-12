from conftest import FakeDndRuntime
from fastapi.testclient import TestClient


def register(client: TestClient, email: str, name: str) -> dict:
    return client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "display_name": name,
        },
    ).json()["user"]


def login(client: TestClient, email: str) -> None:
    client.post(
        "/api/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )


def test_request_and_auto_join_invites(client: TestClient, dnd_runtime: FakeDndRuntime) -> None:
    register(client, "invite-dm@example.com", "DM")
    client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "invite-campaign"},
        json={"name": "Invite Campaign"},
    )
    request_token = client.post(
        "/api/campaigns/campaign-1/invites", json={"mode": "request"}
    ).json()["token"]
    auto_token = client.post(
        "/api/campaigns/campaign-1/invites", json={"mode": "auto_join"}
    ).json()["token"]

    register(client, "invited-1@example.com", "Player 1")
    requested = client.post(
        "/api/invites/accept", json={"token": request_token, "message": "Let me in"}
    )
    assert requested.status_code == 200
    assert requested.json()["status"] == "pending"
    assert not [call for call in dnd_runtime.calls if call[0] == "campaign_access"]

    register(client, "invited-2@example.com", "Player 2")
    joined = client.post("/api/invites/accept", json={"token": auto_token})
    assert joined.status_code == 200, joined.text
    assert joined.json()["status"] == "approved"
    assert len([call for call in dnd_runtime.calls if call[0] == "campaign_access"]) == 1
    assert client.post("/api/invites/accept", json={"token": auto_token}).status_code == 404


def test_admin_quota_grant(client: TestClient) -> None:
    client.app.state.settings.bootstrap_admin_email = "admin@example.com"
    admin = register(client, "admin@example.com", "Admin")
    assert admin["is_admin"] is True
    target = register(client, "quota-target@example.com", "Target")
    denied = client.post(
        f"/api/admin/users/{admin['id']}/quota-grants",
        json={"quantity": 1000},
    )
    assert denied.status_code == 403
    login(client, "admin@example.com")
    granted = client.post(
        f"/api/admin/users/{target['id']}/quota-grants",
        json={"quantity": 5000},
    )
    assert granted.status_code == 201
    assert granted.json()["source"] == "admin"
    login(client, "quota-target@example.com")
    assert client.get("/api/usage/balance").json()["granted"] == "1005000.000000"
