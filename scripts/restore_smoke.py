from __future__ import annotations

import argparse
import json
import time
import uuid

import httpx


def require(response: httpx.Response) -> object:
    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} returned "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18089")
    parser.add_argument("--email", default="owner-e2e@example.com")
    parser.add_argument("--password", default="correct horse battery staple")
    args = parser.parse_args()
    origin = args.base_url.rstrip("/")
    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=120) as client:
        for _ in range(120):
            try:
                if client.get("/api/ready", timeout=10).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("restored SagaSmith Service did not become externally ready")
        require(
            client.post(
                "/api/auth/login",
                json={"email": args.email, "password": args.password},
            )
        )
        campaigns = require(client.get("/api/campaigns"))
        packs = require(client.get("/api/packs"))
        artifacts = require(client.get("/api/community/artifacts"))
        identities = require(client.get("/api/identities"))
        assignments = require(client.get("/api/identities/assignments/mine"))
        audit = require(client.get("/api/admin/audit-events?limit=100"))
        if not isinstance(campaigns, list) or not campaigns:
            raise RuntimeError("restored control database has no campaigns")
        if not isinstance(packs, list) or not packs:
            raise RuntimeError("restored control database has no private Packs")
        if not isinstance(artifacts, list) or not artifacts:
            raise RuntimeError("restored Forge catalog has no Artifacts")
        if not isinstance(identities, list) or not identities:
            raise RuntimeError("restored Forge catalog has no Identities")
        if not isinstance(assignments, list) or not assignments:
            raise RuntimeError("restored control database has no Identity assignments")
        if not isinstance(audit, list) or not any(
            item.get("action") == "campaign.member.revoke" for item in audit
        ):
            raise RuntimeError("restored audit trail is incomplete")
        runtime = require(client.get(f"/api/campaigns/{campaigns[0]['id']}/runtime"))
        import_campaign = None
        dnd_campaigns = [item for item in campaigns if item.get("system_id") == "dnd5e"]
        for campaign in dnd_campaigns:
            imported = require(client.get(f"/api/packs/campaigns/{campaign['id']}"))
            if not any(item.get("private_pack_id") == packs[0]["id"] for item in imported):
                import_campaign = campaign
                break
        if import_campaign is None:
            import_campaign = require(
                client.post(
                    "/api/campaigns",
                    headers={"Idempotency-Key": f"restore-smoke-{uuid.uuid4().hex}"},
                    json={"name": "Restore drill import target", "edition": "2024"},
                )
            )
        pack_import = require(
            client.post(f"/api/packs/{packs[0]['id']}/campaigns/{import_campaign['id']}/import")
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "campaigns": len(campaigns),
                "private_packs": len(packs),
                "artifacts": len(artifacts),
                "identities": len(identities),
                "identity_assignments": len(assignments),
                "audit_events": len(audit),
                "runtime_phase": runtime.get("result", runtime).get("effective_game_phase"),
                "pack_import_status": pack_import.get("status"),
            }
        )
    )


if __name__ == "__main__":
    main()
