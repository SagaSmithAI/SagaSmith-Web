from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi.testclient import TestClient

from sagasmith_service.models import ArtifactRelease, ModuleRun, now_utc
from sagasmith_service.module_worker import BoundedBlockingIo, ModuleJobProcessor

PASSWORD = "correct horse battery staple"


def test_module_blocking_io_runs_off_loop_with_bounded_concurrency() -> None:
    async def exercise() -> None:
        runner = BoundedBlockingIo(1)
        event_loop_thread = threading.get_ident()
        active = 0
        maximum = 0

        def blocking() -> int:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.01)
            active -= 1
            return threading.get_ident()

        threads = await asyncio.gather(
            runner.run("test.read", blocking),
            runner.run("test.read", blocking),
        )
        assert all(thread_id != event_loop_thread for thread_id in threads)
        assert maximum == 1

    asyncio.run(exercise())


def register(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "email": "author@example.com",
            "password": PASSWORD,
            "display_name": "Module Author",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]


def project_payload() -> dict:
    return {
        "slug": "vault-of-glass",
        "title": "Vault of Glass",
        "brief": "Create an original D&D one-shot about a shattered observatory and a dragon.",
        "edition": "2024",
        "locale": "en-US",
        "version": "1.0.0",
        "module_shape": "one_shot",
        "starting_level": 3,
        "ending_level": 3,
        "party_size": 4,
        "session_hours": 4,
        "advancement_mode": "milestone",
    }


def processor(
    client: TestClient, dnd_runtime: FakeDndRuntime, agent_runtime: FakeAgentRuntime
) -> ModuleJobProcessor:
    return ModuleJobProcessor(
        client.app.state.session_factory,
        dnd_runtime,
        agent_runtime,
        client.app.state.private_storage,
        client.app.state.settings,
        worker_id="test-module-worker",
    )


def enqueue(
    client: TestClient, project_id: str, action: str, key: str, payload: dict | None = None
):
    response = client.post(
        f"/api/modules/{project_id}/{action}",
        headers={"Idempotency-Key": key},
        json=payload or {},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_module_studio_worker_pipeline(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    import asyncio

    register(client)
    project = client.post(
        "/api/modules",
        headers={"Idempotency-Key": "module-project-create-2"},
        json=project_payload(),
    ).json()
    runner = processor(client, dnd_runtime, agent_runtime)

    agent_runtime.content = (
        '{"outline":{"premise":"A broken lens threatens the valley",'
        '"acts":["arrival","vault","choice"],"scenes":["village","observatory",'
        '"dragon"],"endings":["repair","destroy"],"risks":[]},"summary":"Playable"}'
    )
    enqueue(client, project["id"], "outline", "module-outline-0002")
    assert asyncio.run(runner.process_one())
    current = client.get(f"/api/modules/{project['id']}").json()
    assert current["status"] == "outline_ready"
    approved = client.post(
        f"/api/modules/{project['id']}/outline-decision",
        json={"approved": True, "feedback": "Proceed"},
    )
    assert approved.status_code == 200

    canonical = "\n".join(
        [
            "# Vault of Glass",
            "## Premise",
            "The broken observatory lens threatens the valley and must be repaired.",
            "## Scene 1: Village",
            "The heroes receive evidence, safety context, and two meaningful routes.",
            "## Scene 2: Observatory",
            "A complete exploration challenge reveals the dragon's actual motive.",
            "## Scene 3: Dragon",
            "The party can negotiate, repair the lens, or fight with a legal ending.",
            "## Endings",
            "Repairing or destroying the lens settles every required module thread.",
        ]
    )
    agent_runtime.content = json_string(
        {
            "canonical_source": canonical,
            "package_decisions": {
                "version": "1.0.0",
                "metadata": {"title": "Vault of Glass"},
            },
            "summary": "Generated",
        }
    )
    enqueue(client, project["id"], "generate", "module-generate-0002")
    assert asyncio.run(runner.process_one())
    assert client.get(f"/api/modules/{project['id']}").json()["status"] == "draft_review"

    agent_runtime.content = json_string(
        {
            "approved": True,
            "summary": "Source-grounded and playable",
            "findings": [],
            "package_decisions": {
                "version": "1.0.0",
                "manifest": {
                    "title": "Vault of Glass",
                    "classification": "adventure",
                    "activation": {"mode": "campaign_attach", "default_active": False},
                    "continuity": {
                        "series_id": None,
                        "order": None,
                        "continues_from": None,
                        "state_policy": {},
                    },
                    "content_summary": {},
                    "play_profile": {
                        "starting_level": {"value": 1, "source_refs": [{"chunk_hash": "abc"}]},
                        "expected_end_level": {
                            "value": 2,
                            "source_refs": [{"chunk_hash": "abc"}],
                        },
                        "advancement": {
                            "modes": ["milestone"],
                            "recommended": "milestone",
                            "source_refs": [{"chunk_hash": "abc"}],
                        },
                        "pregenerated_characters": {
                            "available": False,
                            "applicability": "Reviewed; none are included.",
                            "source_refs": [{"chunk_hash": "abc"}],
                        },
                    },
                },
            },
        }
    )
    enqueue(client, project["id"], "review", "module-review-0002")
    assert asyncio.run(runner.process_one())
    assert client.get(f"/api/modules/{project['id']}").json()["status"] == "ready_to_finalize"

    agent_runtime.content = '{"confirmed":true,"note":"Approved after evidence review."}'
    enqueue(
        client,
        project["id"],
        "finalize",
        "module-finalize-0002",
        {"confirmed": True, "note": "I confirm final publication readiness.", "version": "1.0.0"},
    )
    assert asyncio.run(runner.process_one())
    compiled = client.get(f"/api/modules/{project['id']}").json()
    assert compiled["status"] == "compiled"
    assert compiled["final_checksum"] == "a" * 64
    assert len(agent_runtime.calls[0]["session_id"]) < 160
    immutable = client.post(
        f"/api/modules/{project['id']}/revise",
        headers={"Idempotency-Key": "module-revise-without-version"},
        json={"instruction": "Change the published draft in place"},
    )
    assert immutable.status_code == 422

    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "play-campaign-create-2"},
        json={"name": "Friday Game"},
    )
    assert campaign.status_code == 201, campaign.text
    enqueue(
        client,
        project["id"],
        "install",
        "module-install-0002",
        {"campaign_id": campaign.json()["id"], "activate": True},
    )
    assert asyncio.run(runner.process_one())
    installations = client.get(f"/api/modules/{project['id']}/installations").json()
    assert installations[0]["status"] == "active"

    published = client.post(
        f"/api/modules/{project['id']}/publish",
        json={
            "visibility": "public",
            "license_code": "CC-BY-4.0",
            "rights_attested": True,
            "source_kind": "original",
            "provenance": {"author_statement": "Entirely original"},
            "summary": "An original D&D observatory one-shot.",
            "tags": ["dnd", "one-shot"],
            "changelog": "Initial release",
        },
    )
    assert published.status_code == 201, published.text
    assert published.json()["status"] == "moderation_pending"
    assert published.json()["private_pack_id"] is None
    assert published.json()["module_project_id"] == project["id"]
    published_retry = client.post(
        f"/api/modules/{project['id']}/publish",
        json={
            "visibility": "public",
            "license_code": "CC-BY-4.0",
            "rights_attested": True,
            "source_kind": "original",
            "provenance": {"author_statement": "Entirely original"},
            "summary": "An original D&D observatory one-shot.",
            "tags": ["dnd", "one-shot"],
            "changelog": "Initial release",
        },
    )
    assert published_retry.status_code == 201
    assert published_retry.json()["id"] == published.json()["id"]
    with client.app.state.session_factory() as session:
        release = session.get(ArtifactRelease, published.json()["id"])
        release.status = "published"
        release.published_at = now_utc()
        session.commit()
    community_install = client.post(
        f"/api/community/releases/{published.json()['id']}/install",
        headers={"Idempotency-Key": "community-module-install-0002"},
        json={"campaign_id": campaign.json()["id"], "activate": True},
    )
    assert community_install.status_code == 201, community_install.text
    assert community_install.json()["install_kind"] == "campaign_module"

    notifications = client.get("/api/notifications").json()
    assert len(notifications) >= 5
    calls = [name for name, _ in dnd_runtime.calls]
    assert "module_draft" in calls
    assert "content_pack_import" in calls
    assert "content_pack_activate" in calls


def json_string(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_module_source_rights_and_idempotency(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    register(client)
    headers = {"Idempotency-Key": "module-project-create-3"}
    first = client.post("/api/modules", headers=headers, json=project_payload())
    second = client.post("/api/modules", headers=headers, json=project_payload())
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len([call for call in dnd_runtime.calls if call[0] == "campaign_create"]) == 1
    changed_payload = {**project_payload(), "title": "A Different Module"}
    conflict = client.post("/api/modules", headers=headers, json=changed_payload)
    assert conflict.status_code == 409
    uploaded = client.post(
        f"/api/modules/{first.json()['id']}/sources",
        data={"rights_basis": "reference_only", "license_code": "ARR"},
        files={"source": ("notes.md", b"# Private reference notes", "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["public_eligible"] is False


def test_module_project_tenant_isolation(client: TestClient) -> None:
    register(client)
    created = client.post(
        "/api/modules",
        headers={"Idempotency-Key": "module-project-tenant-owner"},
        json=project_payload(),
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    assert client.post("/api/auth/logout").status_code == 204
    intruder = client.post(
        "/api/auth/register",
        json={
            "email": "intruder@example.com",
            "password": PASSWORD,
            "display_name": "Intruder",
        },
    )
    assert intruder.status_code == 201, intruder.text
    assert client.get(f"/api/modules/{project_id}").status_code == 404
    denied = client.post(
        f"/api/modules/{project_id}/outline",
        headers={"Idempotency-Key": "module-project-tenant-denied"},
        json={},
    )
    assert denied.status_code == 404


def test_module_worker_recovers_expired_lease(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    register(client)
    created = client.post(
        "/api/modules",
        headers={"Idempotency-Key": "module-project-lease-recovery"},
        json=project_payload(),
    )
    run = enqueue(client, created.json()["id"], "outline", "module-outline-lease-recovery")
    with client.app.state.session_factory() as session:
        stored = session.get(ModuleRun, run["id"])
        assert stored is not None
        stored.status = "running"
        stored.lease_owner = "dead-worker"
        stored.lease_expires_at = now_utc() - timedelta(minutes=1)
        session.commit()
    runner = processor(client, dnd_runtime, agent_runtime)
    assert runner.recover_expired() == 1
    with client.app.state.session_factory() as session:
        recovered = session.get(ModuleRun, run["id"])
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_owner is None
        assert recovered.error == "Recovered after worker lease expiry"
