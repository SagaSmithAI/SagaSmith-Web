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
                "schema": "sagasmith.auth-context/v2",
                "target_service": "sagasmith-dnd-mcp",
                "requester_principal": f"user:{user['id']}",
                "acting_host_principal": f"user:{user['id']}",
                "conversation_principal": "agent-conversation:test",
                "tenant_id": "",
                "campaign_id": "campaign-1",
                "tool": "campaign_query",
                "room_turn_id": "turn-1",
                "base_revision": 7,
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
    context = call["context"]
    legacy_context_fields = ("campaign_id", "system_id", "principal_id", "campaign_role")
    assert {name: context[name] for name in legacy_context_fields} == {
        "campaign_id": "campaign-1",
        "system_id": "dnd5e",
        "principal_id": f"user:{user['id']}",
        "campaign_role": "owner",
    }
    authority = context["authority_context"]
    assert authority["schema"] == "sagasmith.auth-context/v2"
    assert authority["target_service"] == "sagasmith-dnd-mcp"
    assert authority["requester_principal"] == f"user:{user['id']}"
    assert authority["resource_owner_principal"] == f"user:{user['id']}"
    assert authority["acting_host_principal"] == f"user:{user['id']}"
    assert authority["authorized_audience"] == "sagasmith-dnd-mcp"
    assert authority["allowed_operations"] == [
        "campaign_query",
        "character_query",
        "module_query",
        "rule_search",
        "skill_query",
    ]
    assert len(authority["allowed_operations"]) <= 16
    assert authority["base_revision"] == 7
    assert authority["room_turn_id"] == response.json()["id"]
    assert authority["idempotency_key"] == f"agent-turn:{response.json()['id']}"
    assert call["idempotency_key"] == authority["idempotency_key"]
    assert authority["conversation_principal"] == f"agent-conversation:{conversation['id']}"
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
        assert audit.details["requester_principal"] == f"user:{user['id']}"
        assert audit.details["acting_host_principal"] == f"user:{user['id']}"

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
