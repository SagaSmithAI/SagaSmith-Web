from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import uuid
import zipfile
from decimal import Decimal

import httpx


def expect(response: httpx.Response, status: int) -> dict:
    if response.status_code != status:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} returned "
            f"{response.status_code}: {response.text}"
        )
    if status == 204:
        return {}
    value = response.json()
    return value if isinstance(value, dict) else {"items": value}


def pack_archive(pack_id: str, version: str) -> bytes:
    document = "# Arrival\nThe party arrives at the lantern gate."
    document_bytes = document.encode("utf-8")
    document_checksum = hashlib.sha256(document_bytes).hexdigest()
    content_hash = document_checksum
    source_key = f"{pack_id}.source"
    chunk_key = f"{source_key}/section-0/chunk-0-{content_hash[:16]}"
    source_ref = {
        "source_key": source_key,
        "chunk_key": chunk_key,
        "page": 1,
        "note": "Container E2E evidence",
    }
    output = io.BytesIO()
    descriptor = {
        "format": "sagasmith.content-package",
        "schema_version": 2,
        "kind": "module",
        "id": pack_id,
        "version": version,
        "system_id": "dnd5e",
        "manifest": {
            "id": pack_id,
            "version": version,
            "system_id": "dnd5e",
            "title": "Private container fixture",
        },
        "dependencies": [],
        "sources": [
            {
                "source_key": source_key,
                "title": "Private container fixture",
                "edition": "2024",
                "locale": "en",
                "version": version,
                "publication_id": "",
                "authority": "user-supplied",
                "normalized_document_asset_key": f"{source_key}.normalized",
                "original_asset_keys": [],
                "sections": [
                    {
                        "ordinal": 0,
                        "parent_ordinal": None,
                        "level": 1,
                        "title": "Arrival",
                        "path": ["Arrival"],
                        "start_offset": 0,
                        "end_offset": len(document),
                        "content_hash": content_hash,
                        "chunks": [
                            {
                                "key": chunk_key,
                                "ordinal": 0,
                                "heading_path": ["Arrival"],
                                "start_offset": 0,
                                "end_offset": len(document),
                                "content_hash": content_hash,
                                "token_count": 9,
                                "page_start": 1,
                                "page_end": 1,
                                "metadata": {},
                            }
                        ],
                    }
                ],
                "metadata": {},
            }
        ],
        "assets": [
            {
                "asset_key": f"{source_key}.normalized",
                "kind": "normalized_document",
                "name": "fixture.md",
                "media_type": "text/markdown",
                "checksum": document_checksum,
                "size": len(document_bytes),
                "blob_key": f"blobs/sha256/{document_checksum}",
                "license": "private",
                "attribution": "Container E2E fixture",
                "source_refs": [],
                "metadata": {"source_key": source_key},
            }
        ],
        "content_reviews": [],
        "actors": [],
        "content": {
            "classification": "adventure",
            "compatibility": {"editions": ["2024"]},
            "play_profile": {
                "starting_level": {"value": 1, "source_refs": [source_ref]},
                "expected_end_level": {"value": 1, "source_refs": [source_ref]},
                "advancement": {
                    "recommended": "milestone",
                    "modes": ["milestone"],
                    "source_refs": [source_ref],
                },
                "pregenerated_characters": {
                    "available": False,
                    "source_refs": [source_ref],
                },
            },
            "continuity": {},
            "activation": {},
            "scene_atlas": [
                {
                    "stable_key": "arrival",
                    "chapter_ordinal": 0,
                    "scene_ordinal": 0,
                    "chapter": "Arrival",
                    "title": "Lantern Gate",
                    "source_span": {
                        "source_key": source_key,
                        "start_offset": 0,
                        "end_offset": len(document),
                    },
                    "scene_type": "exploration",
                    "page_start": 1,
                    "page_end": 1,
                    "headings": ["Arrival"],
                    "keywords": ["gate"],
                    "metadata": {"visibility": "restricted"},
                    "source_refs": [source_ref],
                }
            ],
            "catalogs": {},
            "narrative": {},
        },
        "metadata": {
            "distribution": "private",
            "agent_finalization": {
                "confirmed": True,
                "reviewer": "agent:container-e2e",
                "note": "Synthetic fixture reviewed for deterministic acceptance only.",
            },
        },
    }
    canonical = json.dumps(
        descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    descriptor["checksum"] = hashlib.sha256(canonical).hexdigest()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "package.sagasmith.json",
            json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        archive.writestr(f"blobs/sha256/{document_checksum}", document_bytes)
    return output.getvalue()


def register_or_login(client: httpx.Client, payload: dict[str, str]) -> dict:
    response = client.post("/api/auth/register", json=payload)
    if response.status_code == 201:
        return expect(response, 201)["user"]
    if response.status_code != 409:
        return expect(response, 201)["user"]
    return expect(
        client.post(
            "/api/auth/login",
            json={"email": payload["email"], "password": payload["password"]},
        ),
        200,
    )["user"]


def wait_module_run(
    client: httpx.Client, project_id: str, run_id: str, *, timeout_seconds: int = 180
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        item = expect(client.get(f"/api/modules/{project_id}/runs/{run_id}"), 200)
        if item["status"] == "succeeded":
            return item
        if item["status"] in {"failed", "canceled"}:
            raise RuntimeError(f"Module Studio task did not complete: {item}")
        time.sleep(0.5)
    raise RuntimeError(f"Module Studio task timed out: {run_id}")


def enqueue_module(
    client: httpx.Client,
    project_id: str,
    action: str,
    run_id: str,
    payload: dict | None = None,
) -> dict:
    item = expect(
        client.post(
            f"/api/modules/{project_id}/{action}",
            headers={"Idempotency-Key": f"module-{action}-{run_id}"},
            json=payload or {},
        ),
        202,
    )
    return wait_module_run(client, project_id, item["id"])


def publish_artifact(
    client: httpx.Client,
    *,
    run_id: str,
    artifact_type: str,
    slug: str,
    title: str,
    private_pack_id: str | None = None,
    payload: dict | None = None,
) -> tuple[dict, dict]:
    artifact = expect(
        client.post(
            "/api/community/artifacts",
            json={
                "slug": slug,
                "artifact_type": artifact_type,
                "title": title,
                "summary": "Synthetic original container acceptance fixture.",
                "system_id": "dnd5e",
                "visibility": "public",
                "license_code": "CC-BY-4.0",
                "rights_attested": True,
                "source_kind": "original",
                "provenance": {"fixture": run_id},
                "tags": ["container-e2e"],
            },
        ),
        201,
    )
    release_item = expect(
        client.post(
            f"/api/community/artifacts/{artifact['id']}/releases",
            json={
                "version": "1.0.0",
                "manifest": {"fixture": run_id, "type": artifact_type},
                "payload": payload or {},
                "compatibility": {"edition": "2024"},
                "private_pack_id": private_pack_id,
            },
        ),
        201,
    )
    reviewed = expect(
        client.post(
            f"/api/community/artifacts/{artifact['id']}/releases/{release_item['id']}/agent-review",
            headers={"Idempotency-Key": f"review-{artifact_type}-{run_id}"},
        ),
        200,
    )
    if reviewed["status"] != "agent_reviewed":
        raise RuntimeError(f"hosted Agent did not approve artifact: {reviewed}")
    expect(
        client.post(
            f"/api/community/artifacts/{artifact['id']}/releases/{release_item['id']}/submit"
        ),
        200,
    )
    published = expect(
        client.post(
            f"/api/community/admin/releases/{release_item['id']}/moderate",
            json={"decision": "approved", "notes": "Container acceptance"},
        ),
        200,
    )
    return artifact, published


def run(base_url: str) -> None:
    run_id = uuid.uuid4().hex[:12]
    origin = base_url.rstrip("/")
    owner = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=240)
    player = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=240)
    for _ in range(120):
        try:
            # Narrative readiness launches a real stdio MCP probe, which can take
            # a few seconds on cold Windows/Docker Desktop hosts.
            if owner.get("/api/ready", timeout=10).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("SagaSmith Service did not become ready")
    owner_user = register_or_login(
        owner,
        {
            "email": "owner-e2e@example.com",
            "password": "correct horse battery staple",
            "display_name": "E2E Owner",
        },
    )
    player_user = register_or_login(
        player,
        {
            "email": f"player-{run_id}@example.com",
            "password": "correct horse battery staple",
            "display_name": "E2E Player",
        },
    )
    campaign = expect(
        owner.post(
            "/api/campaigns",
            headers={"Idempotency-Key": f"campaign-{run_id}"},
            json={"name": f"Container E2E {run_id}", "edition": "2024"},
        ),
        201,
    )
    campaign_id = campaign["id"]

    denied = player.post(
        f"/api/campaigns/{campaign_id}/join-requests",
        headers={"Origin": "https://attacker.invalid"},
        json={"message": "cross-site request must fail"},
    )
    expect(denied, 403)
    join_request = expect(
        player.post(
            f"/api/campaigns/{campaign_id}/join-requests",
            json={"message": "container acceptance"},
        ),
        201,
    )
    expect(
        owner.post(
            f"/api/campaigns/{campaign_id}/join-requests/{join_request['id']}/decision",
            json={"decision": "approved"},
        ),
        200,
    )
    runtime = expect(player.get(f"/api/campaigns/{campaign_id}/runtime"), 200)

    conversation = expect(
        player.post(
            f"/api/campaigns/{campaign_id}/agent/conversations",
            json={"title": "Container acceptance"},
        ),
        201,
    )
    message_url = f"/api/campaigns/{campaign_id}/agent/conversations/{conversation['id']}/messages"
    request_key = f"agent-{run_id}"
    run = expect(
        player.post(
            message_url,
            headers={"Idempotency-Key": request_key},
            json={"content": "Confirm the hosted Agent path."},
        ),
        200,
    )
    repeated = expect(
        player.post(
            message_url,
            headers={"Idempotency-Key": request_key},
            json={"content": "Confirm the hosted Agent path."},
        ),
        200,
    )
    if repeated["id"] != run["id"] or run["status"] != "completed":
        raise RuntimeError("Agent idempotency or completion invariant failed")
    if "dynamic MCP call completed" not in (run["assistant_content"] or ""):
        raise RuntimeError(f"unexpected Agent response: {run['assistant_content']!r}")
    ledger = expect(player.get("/api/usage/ledger"), 200)["items"]
    actual_tokens = Decimal(run["prompt_tokens"] + run["completion_tokens"])
    if not ledger or Decimal(ledger[0]["quantity"]) != actual_tokens or actual_tokens <= 0:
        raise RuntimeError(f"Agent usage was not settled: {ledger}")

    coc_campaign = expect(
        owner.post(
            "/api/campaigns",
            headers={"Idempotency-Key": f"coc-campaign-{run_id}"},
            json={
                "name": f"CoC Container E2E {run_id}",
                "system_id": "coc7e",
                "edition": "7e",
            },
        ),
        201,
    )
    coc_campaign_id = coc_campaign["id"]
    coc_conversation = expect(
        owner.post(
            f"/api/campaigns/{coc_campaign_id}/agent/conversations",
            json={"title": "CoC container acceptance"},
        ),
        201,
    )
    coc_run = expect(
        owner.post(
            f"/api/campaigns/{coc_campaign_id}/agent/conversations/"
            f"{coc_conversation['id']}/messages",
            headers={"Idempotency-Key": f"coc-agent-{run_id}"},
            json={"content": "Query the investigator list through the CoC runtime."},
        ),
        200,
    )
    if "dynamic MCP call completed" not in (coc_run["assistant_content"] or ""):
        raise RuntimeError("hosted Agent did not complete a native CoC MCP call")

    narrative_campaign = expect(
        owner.post(
            "/api/campaigns",
            headers={"Idempotency-Key": f"narrative-campaign-{run_id}"},
            json={
                "name": f"Narrative Container E2E {run_id}",
                "system_id": "narrative",
                "edition": "system-neutral",
            },
        ),
        201,
    )
    narrative_campaign_id = narrative_campaign["id"]
    narrative_conversation = expect(
        owner.post(
            f"/api/campaigns/{narrative_campaign_id}/agent/conversations",
            json={"title": "Narrative container acceptance"},
        ),
        201,
    )
    narrative_run = expect(
        owner.post(
            f"/api/campaigns/{narrative_campaign_id}/agent/conversations/"
            f"{narrative_conversation['id']}/messages",
            headers={"Idempotency-Key": f"narrative-agent-{run_id}"},
            json={"content": "Query the Narrative actor list through its native runtime."},
        ),
        200,
    )
    if "dynamic MCP call completed" not in (narrative_run["assistant_content"] or ""):
        raise RuntimeError("hosted Agent did not complete a native Narrative MCP call")

    module_project = expect(
        owner.post(
            "/api/modules",
            headers={"Idempotency-Key": f"module-project-{run_id}"},
            json={
                "slug": f"lantern-gate-{run_id}",
                "title": "Lantern Gate",
                "brief": (
                    "Create an original D&D one-shot about repairing a failing lantern gate."
                ),
                "edition": "2024",
                "locale": "en-US",
                "version": "1.0.0",
                "module_shape": "one_shot",
                "starting_level": 1,
                "ending_level": 1,
                "party_size": 4,
                "session_hours": 4,
                "advancement_mode": "milestone",
            },
        ),
        201,
    )
    project_id = module_project["id"]
    enqueue_module(owner, project_id, "outline", run_id)
    expect(
        owner.post(
            f"/api/modules/{project_id}/outline-decision",
            json={"approved": True, "feedback": "Container acceptance approval"},
        ),
        200,
    )
    enqueue_module(owner, project_id, "generate", run_id)
    enqueue_module(owner, project_id, "review", run_id)
    enqueue_module(
        owner,
        project_id,
        "finalize",
        run_id,
        {
            "confirmed": True,
            "note": "Container acceptance confirms evidence-reviewed finalization.",
            "version": "1.0.0",
        },
    )
    compiled_module = expect(owner.get(f"/api/modules/{project_id}"), 200)
    if (
        compiled_module["status"] != "compiled"
        or not compiled_module["final_artifact"]
        or len(compiled_module["final_checksum"] or "") != 64
    ):
        raise RuntimeError(f"Module Studio did not compile through MCP: {compiled_module}")
    enqueue_module(
        owner,
        project_id,
        "install",
        run_id,
        {"campaign_id": campaign_id, "activate": True},
    )
    installations = expect(owner.get(f"/api/modules/{project_id}/installations"), 200)["items"]
    if not installations or installations[0]["status"] != "active":
        raise RuntimeError(f"Compiled module did not activate: {installations}")

    pack_id = f"private-e2e-{run_id}"
    uploaded = expect(
        owner.post(
            "/api/packs",
            data={
                "pack_id": pack_id,
                "version": "1.0.0",
                "title": "Private container fixture",
                "kind": "module",
                "rights_attested": "true",
            },
            files={
                "archive": (
                    f"{pack_id}.sagasmith-pack",
                    pack_archive(pack_id, "1.0.0"),
                    "application/vnd.sagasmith.content-package+zip",
                )
            },
        ),
        201,
    )
    listed = expect(owner.get("/api/packs"), 200)["items"]
    if uploaded["id"] not in {item["id"] for item in listed}:
        raise RuntimeError("private Pack was not persisted in object storage metadata")
    imported = expect(
        owner.post(f"/api/packs/{uploaded['id']}/campaigns/{campaign_id}/import"),
        200,
    )
    if imported["status"] != "imported":
        raise RuntimeError(f"private Pack did not reach authoritative MCP: {imported}")
    activated = expect(
        owner.post(
            f"/api/packs/{uploaded['id']}/campaigns/{campaign_id}/activate",
            headers={"Idempotency-Key": f"pack-activate-{run_id}"},
        ),
        200,
    )
    if activated["status"] != "activated":
        raise RuntimeError(f"private Pack did not activate authoritatively: {activated}")

    promoted = expect(
        owner.patch(
            f"/api/campaigns/{campaign_id}/members/{player_user['id']}/role",
            json={"role": "dm"},
        ),
        200,
    )
    if promoted["role"] != "dm":
        raise RuntimeError("campaign member was not promoted through MCP authority")

    public_pack_id = f"public-e2e-{run_id}"
    public_pack = expect(
        owner.post(
            "/api/packs",
            data={
                "pack_id": public_pack_id,
                "version": "1.0.0",
                "title": "Original public container fixture",
                "kind": "module",
                "rights_attested": "true",
            },
            files={
                "archive": (
                    f"{public_pack_id}.sagasmith-pack",
                    pack_archive(public_pack_id, "1.0.0"),
                    "application/vnd.sagasmith.content-package+zip",
                )
            },
        ),
        201,
    )
    public_artifact, public_release = publish_artifact(
        owner,
        run_id=run_id,
        artifact_type="module",
        slug=public_pack_id,
        title="Original public container module",
        private_pack_id=public_pack["id"],
    )
    catalog = expect(player.get("/api/community/artifacts?q=public+container"), 200)["items"]
    if public_artifact["id"] not in {item["id"] for item in catalog}:
        raise RuntimeError("published artifact was not visible cross-account")
    public_install = expect(
        player.post(
            f"/api/community/releases/{public_release['id']}/install",
            headers={"Idempotency-Key": f"public-install-{run_id}"},
            json={"campaign_id": campaign_id, "activate": True},
        ),
        201,
    )
    if public_install["status"] != "activated":
        raise RuntimeError(f"public Pack did not activate through MCP: {public_install}")

    demoted = expect(
        owner.patch(
            f"/api/campaigns/{campaign_id}/members/{player_user['id']}/role",
            json={"role": "player"},
        ),
        200,
    )
    if demoted["role"] != "player":
        raise RuntimeError("campaign member was not demoted through MCP authority")

    _soul_artifact, soul_release = publish_artifact(
        owner,
        run_id=run_id,
        artifact_type="soul",
        slug=f"dm-soul-{run_id}",
        title="Container DM Soul",
        payload={"style": "patient", "rules": "MCP authoritative"},
    )
    identity = expect(
        player.post(
            "/api/identities",
            json={
                "handle": f"container-dm-{run_id}",
                "name": "Container DM Identity",
                "identity_kind": "dm",
                "system_id": "dnd5e",
                "bio": "Synthetic hosted identity.",
                "visibility": "public",
                "availability": "available",
                "active_soul_release_id": soul_release["id"],
                "memory_policy": {"campaign_isolation": "required"},
                "public_profile": {},
            },
        ),
        201,
    )
    assignment = expect(
        owner.post(
            f"/api/identities/campaigns/{campaign_id}/invitations",
            headers={"Idempotency-Key": f"identity-invite-{run_id}"},
            json={"identity_id": identity["id"]},
        ),
        201,
    )
    accepted = expect(
        player.post(
            f"/api/identities/assignments/{assignment['id']}/decision",
            json={"decision": "accepted"},
        ),
        200,
    )
    if accepted["status"] != "accepted":
        raise RuntimeError("DM Identity was not granted through MCP")
    expect(
        owner.put(
            f"/api/identities/assignments/{assignment['id']}/memory/current-scene",
            json={
                "content": "The party is at the lantern gate.",
                "audience": "dm",
                "source": "curated",
            },
        ),
        200,
    )
    identity_conversation = expect(
        owner.post(
            f"/api/campaigns/{campaign_id}/agent/conversations",
            json={
                "title": "Hosted DM Identity",
                "identity_assignment_id": assignment["id"],
            },
        ),
        201,
    )
    identity_run = expect(
        owner.post(
            f"/api/campaigns/{campaign_id}/agent/conversations/"
            f"{identity_conversation['id']}/messages",
            headers={"Idempotency-Key": f"identity-message-{run_id}"},
            json={"content": "Query the current character list as the hosted DM."},
        ),
        200,
    )
    if "dynamic MCP call completed" not in (identity_run["assistant_content"] or ""):
        raise RuntimeError("hosted DM Identity did not complete a native MCP call")
    expect(
        owner.put(
            f"/api/campaigns/{campaign_id}/room/host",
            json={"identity_assignment_id": assignment["id"]},
        ),
        200,
    )
    room_turn = expect(
        player.post(
            f"/api/campaigns/{campaign_id}/room/messages",
            headers={"Idempotency-Key": f"identity-room-{run_id}"},
            json={
                "content": "Inspect the scene as a player while the hosted DM responds.",
                "mode": "action",
            },
        ),
        200,
    )
    if (room_turn.get("job") or {}).get("status") != "succeeded":
        raise RuntimeError(f"hosted Identity room turn did not settle: {room_turn}")
    expect(
        owner.put(
            f"/api/campaigns/{campaign_id}/room/host",
            json={"identity_assignment_id": None},
        ),
        200,
    )
    expect(player.delete(f"/api/identities/assignments/{assignment['id']}"), 204)
    expect(
        owner.post(
            f"/api/campaigns/{campaign_id}/agent/conversations/"
            f"{identity_conversation['id']}/messages",
            headers={"Idempotency-Key": f"identity-revoked-{run_id}"},
            json={"content": "This must not run."},
        ),
        404,
    )

    expect(
        owner.delete(f"/api/campaigns/{campaign_id}/members/{player_user['id']}"),
        204,
    )
    expect(player.get(f"/api/campaigns/{campaign_id}/runtime"), 403)
    audit = expect(owner.get("/api/admin/audit-events?limit=200"), 200)["items"]
    actions = {item["action"] for item in audit}
    required = {
        "account.register",
        "campaign.create",
        "campaign.join.approved",
        "agent.complete",
        "campaign.room.agent.complete",
        "pack.upload.private",
        "pack.activate",
        "campaign.member.revoke",
        "campaign.member.role.change",
        "community.release.approved",
        "community.release.install",
        "identity.assignment.accepted",
        "identity.assignment.revoke",
        "identity.memory.write",
        "module.run.finalize.complete",
        "module.run.install.complete",
    }
    if missing := required - actions:
        raise RuntimeError(f"missing audit actions: {sorted(missing)}")
    if not any(item["request_id"] for item in audit):
        raise RuntimeError("audit events have no request correlation")
    narrative_audit = next(
        (
            item
            for item in audit
            if item["action"] == "agent.complete"
            and item["details"].get("campaign_id") == narrative_campaign_id
        ),
        None,
    )
    if narrative_audit is None:
        raise RuntimeError("Narrative Agent completion was not audited")
    narrative_receipts = narrative_audit["details"].get("auth_context_receipts") or []
    if not any(
        receipt.get("schema") == "sagasmith.auth-context/v2"
        and receipt.get("target_service") == "sagasmith-narrative-mcp"
        and receipt.get("requester_principal") == f"user:{owner_user['id']}"
        and receipt.get("acting_host_principal") == f"user:{owner_user['id']}"
        and receipt.get("authorized_audience") == "sagasmith-narrative-mcp"
        and receipt.get("allowed_operations")
        == ["actor_query", "campaign_query", "narrative_query", "skill_query"]
        and receipt.get("campaign_id") == narrative_campaign_id
        and narrative_conversation["id"] in receipt.get("conversation_principal", "")
        and str(receipt.get("tool", "")).endswith("actor_query")
        and receipt.get("base_revision") == 1
        for receipt in narrative_receipts
    ):
        raise RuntimeError(f"Narrative auth-context receipt was not retained: {narrative_receipts}")
    room_audit = next(
        (
            item
            for item in audit
            if item["action"] == "campaign.room.agent.complete"
            and item["details"].get("campaign_id") == campaign_id
        ),
        None,
    )
    if room_audit is None:
        raise RuntimeError("Hosted Identity room completion was not audited")
    room_details = room_audit["details"]
    operations = room_details.get("allowed_operations") or []
    if room_details.get("requester_principal") != f"user:{player_user['id']}":
        raise RuntimeError(f"room requester identity was not preserved: {room_details}")
    if room_details.get("acting_host_principal") != f"agent:{identity['id']}":
        raise RuntimeError(f"hosted acting identity was not preserved: {room_details}")
    if room_details.get("authorized_audience") != "sagasmith-dnd-mcp":
        raise RuntimeError(f"room MCP audience was not exact: {room_details}")
    room_receipts = room_details.get("auth_context_receipts") or []
    if not any(
        receipt.get("schema") == "sagasmith.auth-context/v2"
        and receipt.get("target_service") == "sagasmith-dnd-mcp"
        and receipt.get("requester_principal") == f"user:{player_user['id']}"
        and receipt.get("resource_owner_principal") == f"user:{owner_user['id']}"
        and receipt.get("acting_host_principal") == f"agent:{identity['id']}"
        and receipt.get("authorized_audience") == "sagasmith-dnd-mcp"
        and receipt.get("allowed_operations") == ["character_query"]
        and receipt.get("campaign_id") == campaign_id
        and receipt.get("room_turn_id") == (room_turn.get("job") or {}).get("id")
        and str(receipt.get("tool", "")).endswith("character_query")
        for receipt in room_receipts
    ):
        raise RuntimeError(
            f"Hosted room modern delegation receipt was not retained: {room_receipts}"
        )
    if (
        not operations
        or operations != sorted(set(operations))
        or len(operations) > 16
        or "character_query" not in operations
        or {"exposure", "server_capabilities"}.intersection(operations)
    ):
        raise RuntimeError(f"room MCP facade was not bounded and deterministic: {operations}")
    print(
        json.dumps(
            {
                "status": "ok",
                "owner_user_id": owner_user["id"],
                "player_user_id": player_user["id"],
                "campaign_id": campaign_id,
                "coc_campaign_id": coc_campaign_id,
                "narrative_campaign_id": narrative_campaign_id,
                "phase": runtime.get("result", runtime).get("effective_game_phase"),
                "agent_tokens": run["prompt_tokens"] + run["completion_tokens"],
                "pack_distribution": uploaded["distribution"],
                "public_artifact_id": public_artifact["id"],
                "module_project_id": project_id,
                "identity_id": identity["id"],
                "mcp_protocol": "2026-07-28",
                "authority_contract": "sagasmith.authoritative-mcp/v2",
                "revocation": "enforced",
                "audit_actions": len(actions),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18088")
    args = parser.parse_args()
    run(args.base_url)


if __name__ == "__main__":
    main()
