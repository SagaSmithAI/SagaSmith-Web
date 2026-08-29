from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_nightly_recovery_uses_the_e2e_mcp_delegation_secret() -> None:
    config = json.loads((ROOT / "tests" / "e2e" / "agent-config.json").read_text(encoding="utf-8"))
    servers = config["tools"]["mcpServers"]
    fixture_secrets = {server["delegationSecret"] for server in servers.values()} | {
        server["env"]["SAGASMITH_AUTH_CONTEXT_SECRET"]
        for server in servers.values()
        if server.get("type") == "stdio"
    }
    assert all(server["protocolMode"] == "2026-07-28" for server in servers.values())
    assert all("authContextSecret" not in server for server in servers.values())

    assert len(fixture_secrets) == 1
    fixture_secret = fixture_secrets.pop()
    workflow = (ROOT / ".github" / "workflows" / "nightly-recovery.yml").read_text(encoding="utf-8")

    assert f"SAGASMITH_AUTH_CONTEXT_SECRET: {fixture_secret}" in workflow
