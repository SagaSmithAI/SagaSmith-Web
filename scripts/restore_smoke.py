from __future__ import annotations

import argparse
import json
import time

import httpx


def require(response: httpx.Response) -> object:
    if response.status_code != 200:
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
                if client.get("/api/ready", timeout=2).status_code == 200:
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
        audit = require(client.get("/api/admin/audit-events?limit=100"))
        if not isinstance(campaigns, list) or not campaigns:
            raise RuntimeError("restored control database has no campaigns")
        if not isinstance(packs, list) or not packs:
            raise RuntimeError("restored control database has no private Packs")
        if not isinstance(audit, list) or not any(
            item.get("action") == "campaign.member.revoke" for item in audit
        ):
            raise RuntimeError("restored audit trail is incomplete")
        runtime = require(client.get(f"/api/campaigns/{campaigns[0]['id']}/runtime"))
        import_campaign = None
        for campaign in campaigns:
            imported = require(client.get(f"/api/packs/campaigns/{campaign['id']}"))
            if not any(item.get("private_pack_id") == packs[0]["id"] for item in imported):
                import_campaign = campaign
                break
        if import_campaign is None:
            raise RuntimeError("restore smoke needs a campaign without the newest private Pack")
        pack_import = require(
            client.post(
                f"/api/packs/{packs[0]['id']}/campaigns/{import_campaign['id']}/import"
            )
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "campaigns": len(campaigns),
                "private_packs": len(packs),
                "audit_events": len(audit),
                "runtime_phase": runtime.get("result", runtime).get(
                    "effective_game_phase"
                ),
                "pack_import_status": pack_import.get("status"),
            }
        )
    )


if __name__ == "__main__":
    main()
