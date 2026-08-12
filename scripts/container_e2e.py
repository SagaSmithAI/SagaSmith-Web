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
                    "metadata": {"visibility": "keeper"},
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
            json.dumps(
                descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
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


def run(base_url: str) -> None:
    run_id = uuid.uuid4().hex[:12]
    origin = base_url.rstrip("/")
    owner = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=240)
    player = httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=240)
    for _ in range(120):
        try:
            if owner.get("/api/ready", timeout=2).status_code == 200:
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
    message_url = (
        f"/api/campaigns/{campaign_id}/agent/conversations/"
        f"{conversation['id']}/messages"
    )
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
    demoted = expect(
        owner.patch(
            f"/api/campaigns/{campaign_id}/members/{player_user['id']}/role",
            json={"role": "player"},
        ),
        200,
    )
    if demoted["role"] != "player":
        raise RuntimeError("campaign member was not demoted through MCP authority")

    expect(
        owner.delete(f"/api/campaigns/{campaign_id}/members/{player_user['id']}"),
        204,
    )
    expect(player.get(f"/api/campaigns/{campaign_id}/runtime"), 403)
    audit = expect(owner.get("/api/admin/audit-events?limit=100"), 200)["items"]
    actions = {item["action"] for item in audit}
    required = {
        "account.register",
        "campaign.create",
        "campaign.join.approved",
        "agent.complete",
        "pack.upload.private",
        "pack.activate",
        "campaign.member.revoke",
        "campaign.member.role.change",
    }
    if missing := required - actions:
        raise RuntimeError(f"missing audit actions: {sorted(missing)}")
    if not any(item["request_id"] for item in audit):
        raise RuntimeError("audit events have no request correlation")
    print(
        json.dumps(
            {
                "status": "ok",
                "owner_user_id": owner_user["id"],
                "player_user_id": player_user["id"],
                "campaign_id": campaign_id,
                "phase": runtime.get("result", runtime).get("effective_game_phase"),
                "agent_tokens": run["prompt_tokens"] + run["completion_tokens"],
                "pack_distribution": uploaded["distribution"],
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
