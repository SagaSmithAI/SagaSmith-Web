from typing import Any

from conftest import FakeAgentRuntime
from fastapi.testclient import TestClient
from sqlalchemy import select

from sagasmith_service.models import AuditEvent


def register_and_create_campaign(client: TestClient) -> dict[str, Any]:
    user = client.post(
        "/api/auth/register",
        json={
            "email": "agent@example.com",
            "password": "correct horse battery staple",
            "display_name": "Agent User",
        },
    ).json()["user"]
    response = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "agent-campaign-1"},
        json={"name": "Agent Campaign"},
    )
    assert response.status_code == 201
    return user


def test_agent_call_has_authenticated_scope_and_settles_usage(
    client: TestClient, agent_runtime: FakeAgentRuntime
) -> None:
    user = register_and_create_campaign(client)
    agent_runtime.tool_receipts = (
        {
            "tool": "mcp_sagasmith_dnd_campaign_query",
            "auth_context_receipt": {
                "schema": "sagasmith.auth-context/v1",
                "actor_principal": f"user:{user['id']}",
                "conversation_principal": "session:campaign-1:user:conversation",
                "tenant_id": "",
                "campaign_id": "campaign-1",
                "session_id": "campaign-1:user:conversation",
                "tool": "campaign_query",
                "authorization_epoch": 2,
                "revision": 4,
                "nonce": "agent-receipt-nonce",
            },
        },
    )
    conversation = client.post(
        "/api/campaigns/campaign-1/agent/conversations",
        json={"title": "第一幕"},
    ).json()
    response = client.post(
        f"/api/campaigns/campaign-1/agent/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "agent-message-001"},
        json={"content": "我推开门。"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["assistant_content"] == "你进入了烛堡。"
    call = agent_runtime.calls[0]
    assert call["context"] == {
        "campaign_id": "campaign-1",
        "system_id": "dnd5e",
        "principal_id": f"user:{user['id']}",
        "campaign_role": "owner",
    }
    assert call["session_id"].startswith(f"campaign-1:{user['id']}:")
    balance = client.get("/api/usage/balance").json()
    assert balance["used"] == "150.000000"
    assert balance["reserved"] == "0.000000"
    with client.app.state.session_factory() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "agent.complete"))
        assert audit is not None
        assert audit.details["auth_context_receipts"] == [
            agent_runtime.tool_receipts[0]["auth_context_receipt"]
        ]

    repeated = client.post(
        f"/api/campaigns/campaign-1/agent/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "agent-message-001"},
        json={"content": "我推开门。"},
    )
    assert repeated.status_code == 200
    assert len(agent_runtime.calls) == 1
    mismatch = client.post(
        f"/api/campaigns/campaign-1/agent/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "agent-message-001"},
        json={"content": "不同内容"},
    )
    assert mismatch.status_code == 409


def test_agent_failure_releases_quota(client: TestClient, agent_runtime: FakeAgentRuntime) -> None:
    register_and_create_campaign(client)
    conversation_id = client.post("/api/campaigns/campaign-1/agent/conversations", json={}).json()[
        "id"
    ]
    agent_runtime.fail = True
    response = client.post(
        f"/api/campaigns/campaign-1/agent/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "agent-message-fail"},
        json={"content": "Hello"},
    )
    assert response.status_code == 502
    balance = client.get("/api/usage/balance").json()
    assert balance["used"] == "0.000000"
    assert balance["reserved"] == "0.000000"
