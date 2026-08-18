from __future__ import annotations

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi.testclient import TestClient
from test_community import login, publish, register


def test_dm_identity_invitation_memory_agent_and_revocation(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    client.app.state.settings.bootstrap_admin_email = "admin@forge.example.com"
    register(client, "admin@forge.example.com", "Moderator")
    identity_owner = register(client, "identity@forge.example.com", "Identity Owner")
    _soul, soul_release = publish(
        client,
        agent_runtime,
        artifact_type="soul",
        slug="patient-dm-soul",
        title="Patient DM Soul",
        payload={
            "narrative_style": "patient high fantasy",
            "rulings": "strict deterministic mechanics",
            "safety": ["lines", "veils"],
        },
    )
    login(client, "identity@forge.example.com")
    identity = client.post(
        "/api/identities",
        json={
            "handle": "lantern-dm",
            "name": "Lantern DM",
            "identity_kind": "dm",
            "system_id": "dnd5e",
            "bio": "A patient D&D host.",
            "visibility": "public",
            "availability": "available",
            "active_soul_release_id": soul_release["id"],
            "memory_policy": {"campaign_isolation": "required"},
            "public_profile": {"pace": "slow"},
        },
    )
    assert identity.status_code == 201, identity.text

    owner = register(client, "campaign-owner@forge.example.com", "Campaign Owner")
    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "identity-campaign-create"},
        json={"name": "Identity Campaign"},
    )
    invitation = client.post(
        f"/api/identities/campaigns/{campaign.json()['id']}/invitations",
        headers={"Idempotency-Key": "invite-lantern-dm"},
        json={"identity_id": identity.json()["id"]},
    )
    assert invitation.status_code == 201, invitation.text
    assert invitation.json()["memory_namespace"].startswith(
        f"campaign:{campaign.json()['id']}:identity:{identity.json()['id']}:assignment:"
    )
    duplicate_invitation = client.post(
        f"/api/identities/campaigns/{campaign.json()['id']}/invitations",
        headers={"Idempotency-Key": "invite-lantern-dm-again"},
        json={"identity_id": identity.json()["id"]},
    )
    assert duplicate_invitation.status_code == 409

    login(client, "identity@forge.example.com")
    accepted = client.post(
        f"/api/identities/assignments/{invitation.json()['id']}/decision",
        json={"decision": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    grant = [call for call in dnd_runtime.calls if call[0] == "campaign_access"][-1]
    assert grant[1]["principal_id"] == f"agent:{identity.json()['id']}"
    assert grant[1]["role"] == "dm"

    login(client, "campaign-owner@forge.example.com")
    memory = client.put(
        f"/api/identities/assignments/{invitation.json()['id']}/memory/current-scene",
        json={
            "content": "The party is negotiating at the sealed gate.",
            "audience": "dm",
            "source": "curated",
        },
    )
    assert memory.status_code == 200, memory.text
    stale = client.put(
        f"/api/identities/assignments/{invitation.json()['id']}/memory/current-scene",
        json={
            "content": "Stale overwrite",
            "expected_revision": 99,
        },
    )
    assert stale.status_code == 409

    selected_host = client.put(
        f"/api/campaigns/{campaign.json()['id']}/room/host",
        json={"identity_assignment_id": invitation.json()["id"]},
    )
    assert selected_host.status_code == 200, selected_host.text
    assert selected_host.json()["host_identity_assignment_id"] == invitation.json()["id"]
    hosted_turn = client.post(
        f"/api/campaigns/{campaign.json()['id']}/room/messages",
        headers={"Idempotency-Key": "identity-hosted-room-turn"},
        json={"content": "We offer the guard our sealed letter.", "mode": "action"},
    )
    assert hosted_turn.status_code == 200, hosted_turn.text
    assert hosted_turn.json()["agent_message"]["sender_display_name"] == identity.json()["name"]
    hosted_call = agent_runtime.calls[-1]
    assert hosted_call["context"]["principal_id"] == f"agent:{identity.json()['id']}"
    assert hosted_call["context"]["campaign_role"] == "dm"
    assert hosted_call["session_id"].startswith(f"{campaign.json()['id']}:agent:")

    conversation = client.post(
        f"/api/campaigns/{campaign.json()['id']}/agent/conversations",
        json={
            "title": "Lantern session",
            "identity_assignment_id": invitation.json()["id"],
        },
    )
    assert conversation.status_code == 201, conversation.text
    agent_runtime.content = "The Lantern DM answers from the gate."
    run = client.post(
        f"/api/campaigns/{campaign.json()['id']}/agent/conversations/"
        f"{conversation.json()['id']}/messages",
        headers={"Idempotency-Key": "identity-agent-message"},
        json={"content": "We offer the guard our sealed letter."},
    )
    assert run.status_code == 200, run.text
    call = agent_runtime.calls[-1]
    assert call["context"]["principal_id"] == f"agent:{identity.json()['id']}"
    assert call["context"]["soul"]["narrative_style"] == "patient high fantasy"
    assert call["context"]["campaign_memory"][0]["key"] == "current-scene"
    assert call["session_id"].startswith(f"{campaign.json()['id']}:agent:")

    login(client, "identity@forge.example.com")
    revoked = client.delete(f"/api/identities/assignments/{invitation.json()['id']}")
    assert revoked.status_code == 204, revoked.text
    revoke = [call for call in dnd_runtime.calls if call[0] == "campaign_access_revoke"][-1]
    assert revoke[1]["principal_id"] == f"agent:{identity.json()['id']}"
    login(client, "campaign-owner@forge.example.com")
    denied = client.post(
        f"/api/campaigns/{campaign.json()['id']}/agent/conversations/"
        f"{conversation.json()['id']}/messages",
        headers={"Idempotency-Key": "identity-after-revoke"},
        json={"content": "Continue."},
    )
    assert denied.status_code == 404
    assert owner["id"] != identity_owner["id"]


def test_keeper_identity_uses_coc_campaign_runtime(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    client.app.state.settings.bootstrap_admin_email = "admin@forge.example.com"
    register(client, "admin@forge.example.com", "Keeper Moderator")
    register(client, "keeper-owner@example.com", "Keeper Owner")
    _soul, soul_release = publish(
        client,
        agent_runtime,
        artifact_type="soul",
        slug="keeper-soul",
        title="Keeper Soul",
        payload={"voice": "measured cosmic horror"},
        system_id="coc7e",
    )
    login(client, "keeper-owner@example.com")
    identity = client.post(
        "/api/identities",
        json={
            "handle": "lantern-keeper",
            "name": "Lantern Keeper",
            "identity_kind": "keeper",
            "system_id": "coc7e",
            "visibility": "public",
            "availability": "available",
            "active_soul_release_id": soul_release["id"],
        },
    )
    assert identity.status_code == 201, identity.text

    client.app.state.coc_runtime = dnd_runtime
    client.app.state.game_runtimes["coc7e"] = dnd_runtime
    register(client, "coc-table@example.com", "CoC Table")
    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "keeper-campaign-create"},
        json={"name": "Arkham Table", "system_id": "coc7e"},
    )
    assert campaign.status_code == 201, campaign.text
    invitation = client.post(
        f"/api/identities/campaigns/{campaign.json()['id']}/invitations",
        headers={"Idempotency-Key": "invite-lantern-keeper"},
        json={"identity_id": identity.json()["id"]},
    )
    assert invitation.status_code == 201, invitation.text

    login(client, "keeper-owner@example.com")
    accepted = client.post(
        f"/api/identities/assignments/{invitation.json()['id']}/decision",
        json={"decision": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    grant = [call for call in dnd_runtime.calls if call[0] == "campaign_access"][-1]
    assert grant[1]["principal_id"] == f"agent:{identity.json()['id']}"
    assert grant[1]["role"] == "dm"


def test_moderation_suspends_identity_and_revokes_mcp_grant(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    client.app.state.settings.bootstrap_admin_email = "admin@forge.example.com"
    register(client, "admin@forge.example.com", "Moderator")
    register(client, "host@example.com", "Host")
    _soul, soul_release = publish(
        client,
        agent_runtime,
        artifact_type="soul",
        slug="reported-soul",
        title="Reported Soul",
        payload={"voice": "measured", "boundaries": ["mcp-authority"]},
    )
    login(client, "host@example.com")
    identity = client.post(
        "/api/identities",
        json={
            "handle": "reported-host",
            "name": "Reported Host",
            "identity_kind": "dm",
            "system_id": "dnd5e",
            "visibility": "public",
            "availability": "available",
            "active_soul_release_id": soul_release["id"],
        },
    ).json()

    register(client, "table@example.com", "Table Owner")
    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "moderation-campaign"},
        json={"name": "Moderated Table"},
    ).json()
    invitation = client.post(
        f"/api/identities/campaigns/{campaign['id']}/invitations",
        headers={"Idempotency-Key": "moderation-invite"},
        json={"identity_id": identity["id"]},
    ).json()
    login(client, "host@example.com")
    assert (
        client.post(
            f"/api/identities/assignments/{invitation['id']}/decision",
            json={"decision": "accepted"},
        ).status_code
        == 200
    )

    report = client.post(
        "/api/community/reports",
        json={
            "target_type": "identity",
            "target_id": identity["id"],
            "reason": "privacy",
            "details": "The public profile exposes private personal material.",
        },
    ).json()
    login(client, "admin@forge.example.com")
    decision = client.post(
        f"/api/community/admin/reports/{report['id']}/decision",
        json={"status": "resolved", "resolution": "Verified and suspended."},
    )
    assert decision.status_code == 200, decision.text
    assert client.get(f"/api/identities/{identity['id']}").json()["status"] == "suspended"
    revoke = [call for call in dnd_runtime.calls if call[0] == "campaign_access_revoke"][-1]
    assert revoke[1]["principal_id"] == f"agent:{identity['id']}"

    login(client, "table@example.com")
    assignments = client.get("/api/identities/assignments/mine").json()
    assert assignments[0]["status"] == "revoked"
