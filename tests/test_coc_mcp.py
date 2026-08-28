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


def test_coc_combat_start_bulk_loads_character_revisions_once(monkeypatch) -> None:
    runtime = StreamableHttpCocRuntime("http://coc.invalid/mcp")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(
        name: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
        campaign_id: str | None,
    ) -> dict[str, Any]:
        calls.append((name, arguments))
        assert principal_id == "user:keeper"
        assert campaign_id == "campaign-coc"
        if name == "character_query":
            assert arguments["action"] == "list"
            return {
                "result": {
                    "characters": [
                        {"id": "investigator-1", "campaign_id": campaign_id, "revision": 4},
                        {"id": "cultist-1", "campaign_id": campaign_id, "revision": 9},
                    ]
                }
            }
        if name == "combat_start":
            return {"result": {"combat": {"active": True}}}
        raise AssertionError(name)

    monkeypatch.setattr(runtime, "_call", fake_call)

    result = asyncio.run(
        runtime.start_combat(
            campaign_id="campaign-coc",
            principal_id="user:keeper",
            participant_ids=["investigator-1", "cultist-1"],
            participant_config=[
                {"actor_id": "investigator-1", "position": {"x": 2, "y": 3}},
                {"actor_id": "cultist-1", "side": "hostiles", "position": {"x": 8, "y": 3}},
            ],
            positioning_mode="grid",
            name="Library ambush",
            expected_revision=12,
            idempotency_key="combat-start-1",
        )
    )

    assert result["result"]["combat"]["active"] is True
    assert [name for name, _ in calls] == ["character_query", "combat_start"]
    payload = calls[-1][1]
    assert payload["expected_character_revisions"] == {
        "investigator-1": 4,
        "cultist-1": 9,
    }
    assert payload["participants"][0]["position"] == {"x": 2, "y": 3}
