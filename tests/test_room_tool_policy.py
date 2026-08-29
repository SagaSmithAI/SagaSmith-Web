from __future__ import annotations

import json
from pathlib import Path

import pytest

from sagasmith_service.room_tool_policy import (
    MAX_ROOM_TURN_TOOLS,
    MCP_SERVICE_BY_SYSTEM,
    RoomToolPolicyError,
    campaign_phase_and_revision,
    policy_matrix,
    select_room_turn_tools,
    service_for_system,
)

ROOT = Path(__file__).resolve().parents[1]


def _fixture() -> dict:
    return json.loads(
        (ROOT / "tests/fixtures/agent-modern-worker-contract.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "narrative"])
def test_each_system_uses_exact_service_audience_and_bounded_sorted_tools(system_id: str) -> None:
    fixture = _fixture()
    assert service_for_system(system_id) == fixture["services"][system_id]
    phases = {key[1] for key in policy_matrix() if key[0] == system_id and key[2] == "action"}
    assert phases
    for phase in phases:
        tools = select_room_turn_tools(
            system_id=system_id,
            phase=phase,
            role="player",
            task="action",
        )
        assert tools == tuple(sorted(set(tools)))
        assert 0 < len(tools) <= MAX_ROOM_TURN_TOOLS
        assert len(tools) <= fixture["max_allowed_operations"]
        assert all("." not in tool and tool != "*" for tool in tools)
        assert not {"exposure", "server_capabilities"}.intersection(tools)


def test_policy_matrix_covers_all_reviewed_system_phases_and_tasks() -> None:
    matrix = policy_matrix()
    expected_phases = {
        "dnd5e": {"lobby", "play", "combat"},
        "coc7e": {"lobby", "play", "combat"},
        "narrative": {"lobby", "play", "conflict"},
    }
    for system_id, phases in expected_phases.items():
        for phase in phases:
            for task in ("chat", "action", "narration"):
                assert (system_id, phase, task) in matrix
    for (system_id, phase, task), expected in matrix.items():
        role = "owner" if task == "narration" else "player"
        selected = select_room_turn_tools(
            system_id=system_id,
            phase=phase,
            role=role,
            task=task,
        )
        assert selected == expected
        assert selected == tuple(sorted(set(selected)))
        assert 1 <= len(selected) <= MAX_ROOM_TURN_TOOLS


def test_player_cannot_receive_narration_tools() -> None:
    with pytest.raises(RoomToolPolicyError, match="narration requires"):
        select_room_turn_tools(
            system_id="dnd5e",
            phase="play",
            role="player",
            task="narration",
        )


@pytest.mark.parametrize(
    ("system_id", "phase", "role", "task"),
    [
        ("unknown", "play", "player", "action"),
        ("dnd5e", "unknown", "player", "action"),
        ("dnd5e", "play", "spectator", "action"),
        ("dnd5e", "play", "player", "unknown"),
    ],
)
def test_unknown_or_zero_match_policy_fails_closed(
    system_id: str, phase: str, role: str, task: str
) -> None:
    with pytest.raises(RoomToolPolicyError):
        select_room_turn_tools(
            system_id=system_id,
            phase=phase,
            role=role,
            task=task,
        )


def test_service_map_matches_agent_contract_fixture() -> None:
    assert MCP_SERVICE_BY_SYSTEM == _fixture()["services"]


@pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "narrative"])
def test_real_domain_campaign_fixtures_normalize_phase_and_revision(system_id: str) -> None:
    fixtures = json.loads(
        (ROOT / "tests/fixtures/domain-campaign-states.json").read_text(encoding="utf-8")
    )
    fixture = fixtures[system_id]
    phase, revision = campaign_phase_and_revision(system_id, fixture["response"])
    assert {"phase": phase, "revision": revision} == fixture["expected"]


def test_revision_zero_is_authoritative_not_missing() -> None:
    assert campaign_phase_and_revision(
        "dnd5e",
        {"result": {"revision": 0, "campaign_revision": 99, "effective_game_phase": "lobby"}},
    ) == ("lobby", 0)
