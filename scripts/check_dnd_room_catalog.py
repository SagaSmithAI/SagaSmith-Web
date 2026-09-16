"""Verify every Web D&D room selection against a real built Runtime catalog.

Read the JSON emitted by sagasmith_dnd_runtime.publish/catalog export on stdin.
This intentionally does not ship a duplicate hand-maintained tool-name fixture.
"""

from __future__ import annotations

import json
import sys

from sagasmith_service.room_tool_policy import (
    MAX_ROOM_TURN_TOOLS,
    policy_matrix,
    select_room_turn_tools,
)


def check_catalog(rows: list[dict]) -> dict:
    catalog = {row["id"]: row for row in rows}
    count = 0
    for system, phase, task in policy_matrix():
        if system != "dnd5e":
            continue
        for role in ("owner", "dm", "player"):
            if task == "narration" and role == "player":
                continue
            selected = select_room_turn_tools(system_id=system, phase=phase, role=role, task=task)
            assert 0 < len(selected) <= MAX_ROOM_TURN_TOOLS
            missing = set(selected) - catalog.keys()
            assert not missing, f"{phase}/{role}/{task}: unknown Runtime operations {missing}"
            for name in selected:
                # The owner/DM may start combat and read its result in this turn.
                after_start = phase == "play" and name == "combat_query" and (
                    role in {"owner", "dm"} and "combat_start" in selected
                )
                assert after_start or phase in catalog[name]["phases"], (
                    f"{phase}/{role}/{task}: {name} unavailable"
                )
            count += 1
    combat = select_room_turn_tools(
        system_id="dnd5e", phase="combat", role="dm", task="action",
    )
    assert {"combat_check", "combat_choice"} <= set(combat)
    assert "combat_reaction_defense" not in combat
    roll = select_room_turn_tools(system_id="dnd5e", phase="combat", role="dm", task="roll")
    assert {"combat_check", "dnd_check", "dnd_dice_roll"} <= set(roll)
    return {"status": "ok", "runtime_operations": len(catalog), "room_selections": count,
            "combat_action_tools": len(combat)}


if __name__ == "__main__":
    print(json.dumps(check_catalog(json.load(sys.stdin))))
