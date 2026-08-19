from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_config(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def test_regression_agent_config_uses_current_hosted_topology() -> None:
    example = _read_config("agent-config.example.json")
    regression = _read_config("agent-config.regression.json")

    assert regression["tools"] == example["tools"]
    assert (
        regression["agents"]["defaults"]["externalSkillsDirs"]
        == example["agents"]["defaults"]["externalSkillsDirs"]
    )
    assert regression["api"] == example["api"]
    assert regression["modelPresets"]["hosted"]["model"] == "gpt-5.6-luna"
