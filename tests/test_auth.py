from fastapi.testclient import TestClient


def register(client: TestClient, email: str = "dm@example.com"):
    return client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": "Dungeon Master",
        },
    )


def test_register_me_logout_and_login(client: TestClient) -> None:
    created = register(client)
    assert created.status_code == 201
    assert created.json()["user"]["principal_id"].startswith("user:")
    assert client.get("/api/auth/me").status_code == 200

    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    logged_in = client.post(
        "/api/auth/login",
        json={
            "email": "DM@EXAMPLE.COM",
            "password": "correct-horse-battery-staple",
        },
    )
    assert logged_in.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_duplicate_registration_and_wrong_password_are_bounded(client: TestClient) -> None:
    assert register(client).status_code == 201
    assert register(client).status_code == 409

    rejected = client.post(
        "/api/auth/login",
        json={"email": "dm@example.com", "password": "not-the-password"},
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid email or password"
