from fastapi.testclient import TestClient

PASSWORD = "correct horse battery staple"


def register(client: TestClient, email: str, name: str, **headers: str) -> dict:
    response = client.post(
        "/api/auth/register",
        headers=headers,
        json={"email": email, "password": PASSWORD, "display_name": name},
    )
    assert response.status_code == 201
    return response.json()["user"]


def login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def test_admin_can_filter_request_correlated_audit_events(client: TestClient) -> None:
    client.app.state.settings.bootstrap_admin_email = "audit-admin@example.com"
    register(client, "audit-admin@example.com", "Audit Admin")
    target = register(
        client,
        "audit-target@example.com",
        "Audit Target",
        **{"X-Request-ID": "audit-register-001"},
    )

    denied = client.get("/api/admin/audit-events")
    assert denied.status_code == 403

    login(client, "audit-admin@example.com")
    response = client.get(
        "/api/admin/audit-events",
        params={"action": "account.register", "subject_id": target["id"]},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["request_id"] == "audit-register-001"


def test_logout_is_audited(client: TestClient) -> None:
    client.app.state.settings.bootstrap_admin_email = "logout-admin@example.com"
    admin = register(client, "logout-admin@example.com", "Logout Admin")
    response = client.post("/api/auth/logout", headers={"X-Request-ID": "audit-logout-001"})
    assert response.status_code == 204
    login(client, "logout-admin@example.com")
    events = client.get(
        "/api/admin/audit-events",
        params={"action": "account.logout"},
    ).json()
    assert events[0]["actor_user_id"] == admin["id"]
    assert events[0]["request_id"] == "audit-logout-001"
