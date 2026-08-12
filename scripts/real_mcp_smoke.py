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
    campaign_id = str(
        created_result.get("id") or created_result.get("campaign_id") or ""
    )
    if not campaign_id:
        raise RuntimeError(f"campaign_create returned no campaign id: {created}")
    queried = await runtime.get_campaign(
        campaign_id=campaign_id, principal_id="user:service-smoke"
    )
    result = queried.get("result", queried)
    if str(result.get("id") or "") != campaign_id:
        raise RuntimeError(f"campaign_query did not return {campaign_id}: {queried}")
    print(
        {
            "status": "ok",
            "campaign_id": campaign_id,
            "phase": result.get("effective_game_phase"),
            "revision": result.get("revision"),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8767/mcp")
    arguments = parser.parse_args()
    asyncio.run(smoke(arguments.url))


if __name__ == "__main__":
    main()
