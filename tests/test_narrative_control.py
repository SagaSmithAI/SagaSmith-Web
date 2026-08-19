from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from sagasmith_service.narrative_control import NarrativeControlClient

WORKSPACE = Path(__file__).resolve().parents[2]
NARRATIVE = WORKSPACE / "sagasmith-narrative"
NARRATIVE_PYTHON = NARRATIVE / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SECRET = "hosted-narrative-control-secret-at-least-32-bytes"


@pytest.mark.skipif(not NARRATIVE_PYTHON.exists(), reason="Narrative workspace runtime unavailable")
def test_real_narrative_control_parallel_campaigns_and_process_recovery(tmp_path: Path) -> None:
    config = tmp_path / "agent-config.json"
    config.write_text(
        json.dumps(
            {
                "tools": {
                    "mcpServers": {
                        "sagasmith_narrative": {
                            "type": "stdio",
                            "command": str(NARRATIVE_PYTHON),
                            "args": ["-m", "sagasmith_narrative_mcp.server"],
                            "cwd": str(NARRATIVE),
                            "env": {
                                "SAGASMITH_NARRATIVE_MCP_HOME": str(tmp_path / "state"),
                                "SAGASMITH_AUTH_CONTEXT_SECRET": SECRET,
                            },
                            "authContextSecret": SECRET,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def scenario() -> None:
        control = NarrativeControlClient.from_agent_config(str(config))
        await control.probe()
        first, second = await asyncio.gather(
            control.execute(
                "create_campaign",
                {
                    "name": "Narrative One",
                    "description": "first",
                    "principal_id": "user:owner-one",
                    "idempotency_key": "service:narrative:first",
                },
            ),
            control.execute(
                "create_campaign",
                {
                    "name": "Narrative Two",
                    "description": "second",
                    "principal_id": "user:owner-two",
                    "idempotency_key": "service:narrative:second",
                },
            ),
        )
        assert first["id"] != second["id"]

        restarted = NarrativeControlClient.from_agent_config(str(config))
        recovered_first, recovered_second = await asyncio.gather(
            restarted.execute(
                "get_campaign",
                {"campaign_id": first["id"], "principal_id": "user:owner-one"},
            ),
            restarted.execute(
                "get_campaign",
                {"campaign_id": second["id"], "principal_id": "user:owner-two"},
            ),
        )
        assert recovered_first["name"] == "Narrative One"
        assert recovered_second["name"] == "Narrative Two"
        assert recovered_first["id"] != recovered_second["id"]
        changed = await restarted.execute(
            "set_game_phase",
            {
                "campaign_id": first["id"],
                "principal_id": "user:owner-one",
                "phase": "lobby",
                "expected_revision": recovered_first["revision"],
                "idempotency_key": "service:narrative:first:play",
            },
        )
        receipt = changed["auth_context_receipt"]
        assert receipt["actor_principal"] == "user:owner-one"
        assert receipt["campaign_id"] == first["id"]
        assert receipt["tool"] == "game_phase"
        assert receipt["revision"] is not None

    asyncio.run(scenario())
