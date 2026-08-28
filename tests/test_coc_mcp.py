from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from sagasmith_service.integrations.coc_mcp import StreamableHttpCocRuntime


def test_coc_runtime_unwraps_audience_safe_host_projections(monkeypatch) -> None:
    runtime = StreamableHttpCocRuntime("http://coc.invalid/mcp")

    async def fake_call(
        name: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
        campaign_id: str | None,
    ) -> dict[str, Any]:
        assert principal_id == "user:player"
        if name == "campaign_query":
            return {
                "result": {
                    "id": "campaign-coc",
                    "revision": 9,
                    "effective_game_phase": "combat",
                    "state": {"game_phase": "combat", "combat": {"active": True}},
                }
            }
        if name == "character_query" and arguments["action"] == "list":
            return {
                "result": {
                    "characters": [
                        {
                            "id": "investigator-1",
                            "campaign_id": "campaign-coc",
                            "name": "Armitage",
                        }
                    ]
                }
            }
        if name == "character_query":
            return {
                "result": {
                    "id": "investigator-1",
                    "campaign_id": "campaign-coc",
                    "name": "Armitage",
                }
            }
        if name == "resolution_presentation":
            return {
                "result": {
                    "schema": "sagasmith.resolution-presentation/v1",
                    "resolution_id": arguments["resolution_id"],
                    "system_id": "coc7e",
                    "campaign_id": "campaign-coc",
                }
            }
        raise AssertionError(name)

    @asynccontextmanager
    async def fake_request_session(**_arguments: Any):
        yield lambda name, arguments: fake_call(
            name,
            arguments,
            principal_id="user:player",
            campaign_id="campaign-coc",
        )

    monkeypatch.setattr(runtime, "_request_session", fake_request_session)
    monkeypatch.setattr(runtime, "_call", fake_call)

    async def exercise() -> None:
        panel = await runtime.get_panel_state(
            campaign_id="campaign-coc", principal_id="user:player"
        )
        assert panel["phase"] == "combat"
        assert panel["revision"] == 9
        assert panel["characters"][0]["id"] == "investigator-1"
        assert panel["combat"] == {"active": True}
        unchanged = await runtime.get_panel_state(
            campaign_id="campaign-coc",
            principal_id="user:player",
            known_revision=9,
        )
        assert unchanged == {"not_modified": True, "revision": 9}
        actor = await runtime.get_character_card(
            campaign_id="campaign-coc",
            character_id="investigator-1",
            principal_id="user:player",
        )
        assert actor["name"] == "Armitage"
        presentation = await runtime.get_resolution_presentation(
            campaign_id="campaign-coc",
            resolution_id="resolution-1",
            principal_id="user:player",
        )
        assert presentation["resolution_id"] == "resolution-1"

    asyncio.run(exercise())
