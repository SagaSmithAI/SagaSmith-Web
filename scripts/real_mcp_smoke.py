from __future__ import annotations

import argparse
import asyncio
import uuid

from sagasmith_service.integrations.dnd_mcp import StreamableHttpDndRuntime


async def smoke(url: str) -> None:
    runtime = StreamableHttpDndRuntime(url)
    run_id = uuid.uuid4()
    request_key = f"service-smoke:{run_id}"
    created = await runtime.create_campaign(
        name=f"SagaSmith Service Smoke {run_id.hex[:12]}",
        description="isolated public-facade integration test",
        edition="2024",
        locale="zh-CN",
        advancement_mode="milestone",
        principal_id="user:service-smoke",
        idempotency_key=request_key,
    )
    created_result = created.get("result", created)
    campaign_id = str(created_result.get("id") or created_result.get("campaign_id") or "")
    if not campaign_id:
        raise RuntimeError(f"campaign_create returned no campaign id: {created}")
    queried = await runtime.get_campaign(campaign_id=campaign_id, principal_id="user:service-smoke")
    result = queried.get("result", queried)
    if str(result.get("id") or "") != campaign_id:
        raise RuntimeError(f"campaign_query did not return {campaign_id}: {queried}")
    removed_principal = f"user:service-smoke-removed:{run_id}"
    await runtime.grant_campaign_access(
        campaign_id=campaign_id,
        principal_id=removed_principal,
        role="player",
        by_principal_id="user:service-smoke",
    )
    revoked = await runtime.revoke_campaign_access(
        campaign_id=campaign_id,
        principal_id=removed_principal,
        by_principal_id="user:service-smoke",
    )
    revoked_result = revoked.get("result", revoked)
    if revoked_result.get("revoked") is not True:
        raise RuntimeError(f"access_revoke did not revoke the player: {revoked}")
    try:
        await runtime.get_campaign(
            campaign_id=campaign_id,
            principal_id=removed_principal,
        )
    except RuntimeError as exc:
        if "cannot access campaign" not in str(exc):
            raise
    else:
        raise RuntimeError("revoked player retained authoritative campaign access")
    print(
        {
            "status": "ok",
            "campaign_id": campaign_id,
            "phase": result.get("effective_game_phase"),
            "revision": result.get("revision"),
            "revocation": "enforced",
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8767/mcp")
    arguments = parser.parse_args()
    asyncio.run(smoke(arguments.url))


if __name__ == "__main__":
    main()
