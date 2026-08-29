"""Conservative Host-side MCP facade selection for durable room turns.

The domain MCP catalog remains authoritative.  This module only chooses a
small, stable subset of exact public tool IDs for the model from trusted Host
state.  Domain servers still re-check role, phase, revision, actor authority,
and idempotency on every call.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_ROOM_TURN_TOOLS = 16

MCP_SERVICE_BY_SYSTEM = {
    "dnd5e": "sagasmith-dnd-mcp",
    "coc7e": "sagasmith-coc-mcp",
    "narrative": "sagasmith-narrative-mcp",
}

_ROLES = frozenset({"owner", "dm", "player"})
_DM_ROLES = frozenset({"owner", "dm"})
_TASKS = frozenset({"chat", "action", "narration"})


class RoomToolPolicyError(ValueError):
    """Raised when trusted room state has no reviewed, model-visible subset."""


@dataclass(frozen=True)
class _PolicyKey:
    system_id: str
    phase: str
    task: str


def _ids(value: str) -> tuple[str, ...]:
    return tuple(sorted(value.split()))


_POLICIES: dict[_PolicyKey, tuple[str, ...]] = {
    # D&D 5e
    _PolicyKey("dnd5e", "lobby", "chat"): _ids(
        "campaign_query character_query module_query skill_query"
    ),
    _PolicyKey("dnd5e", "play", "chat"): _ids(
        "campaign_query character_query module_query rule_search skill_query"
    ),
    _PolicyKey("dnd5e", "combat", "chat"): _ids(
        "campaign_query character_query combat_query rule_search skill_query"
    ),
    _PolicyKey("dnd5e", "lobby", "action"): _ids(
        "campaign_query character_action character_query dnd_dice_roll "
        "resolution_presentation skill_query"
    ),
    _PolicyKey("dnd5e", "play", "action"): _ids(
        "campaign_query character_action character_check character_query dnd_check "
        "dnd_dice_roll resolution_presentation skill_query"
    ),
    _PolicyKey("dnd5e", "combat", "action"): _ids(
        "campaign_query character_query combat_cast_spell combat_choice "
        "combat_common_action combat_end_turn combat_movement combat_preflight_attack "
        "combat_query combat_reaction_attack combat_resolve_attack combat_use_activity "
        "dnd_check dnd_dice_roll resolution_presentation skill_query"
    ),
    _PolicyKey("dnd5e", "lobby", "narration"): _ids(
        "campaign_change campaign_event campaign_query module_query module_set_progress "
        "snapshot_create skill_query"
    ),
    _PolicyKey("dnd5e", "play", "narration"): _ids(
        "campaign_change campaign_event campaign_query module_query module_set_progress "
        "npc_conversation snapshot_create skill_query"
    ),
    _PolicyKey("dnd5e", "combat", "narration"): _ids(
        "campaign_query combat_end combat_join combat_map_patch combat_query snapshot_create "
        "skill_query"
    ),
    # Call of Cthulhu 7e
    _PolicyKey("coc7e", "lobby", "chat"): _ids(
        "campaign_query character_query module_query skill_query"
    ),
    _PolicyKey("coc7e", "play", "chat"): _ids(
        "campaign_query character_query investigation_query rule_query skill_query"
    ),
    _PolicyKey("coc7e", "combat", "chat"): _ids(
        "campaign_query character_query combat_query rule_query skill_query"
    ),
    _PolicyKey("coc7e", "lobby", "action"): _ids(
        "campaign_query character_change character_query coc_dice_roll coc_resolve "
        "resolution_presentation skill_query"
    ),
    _PolicyKey("coc7e", "play", "action"): _ids(
        "campaign_query character_change character_query coc_dice_roll coc_hp_change "
        "coc_resolve coc_sanity_check investigation_check investigation_query "
        "resolution_presentation skill_query"
    ),
    _PolicyKey("coc7e", "combat", "action"): _ids(
        "campaign_query character_change character_query coc_dice_roll coc_hp_change "
        "coc_resolve coc_sanity_check combat_action combat_attack combat_query "
        "resolution_presentation skill_query"
    ),
    _PolicyKey("coc7e", "lobby", "narration"): _ids(
        "campaign_event campaign_query module_change module_query snapshot_change skill_query"
    ),
    _PolicyKey("coc7e", "play", "narration"): _ids(
        "campaign_change campaign_event campaign_query chase_end chase_start combat_start "
        "group_luck_check module_change npc_conversation snapshot_change skill_query"
    ),
    _PolicyKey("coc7e", "combat", "narration"): _ids(
        "campaign_query combat_end combat_query snapshot_change skill_query"
    ),
    # System-neutral Narrative.
    _PolicyKey("narrative", "lobby", "chat"): _ids(
        "actor_query campaign_query narrative_query skill_query"
    ),
    _PolicyKey("narrative", "play", "chat"): _ids(
        "actor_query campaign_query continuity_query narrative_query skill_query"
    ),
    _PolicyKey("narrative", "conflict", "chat"): _ids(
        "actor_query campaign_query conflict_query continuity_query narrative_query skill_query"
    ),
    _PolicyKey("narrative", "lobby", "action"): _ids(
        "actor_change actor_query campaign_query narrative_query skill_query"
    ),
    _PolicyKey("narrative", "play", "action"): _ids(
        "actor_change actor_query campaign_query mechanic_resolve narrative_query "
        "narrative_settle skill_query"
    ),
    _PolicyKey("narrative", "conflict", "action"): _ids(
        "actor_query campaign_query conflict_act conflict_query continuity_query "
        "mechanic_resolve narrative_query skill_query"
    ),
    _PolicyKey("narrative", "lobby", "narration"): _ids(
        "campaign_query narrative_query pack_change profile_change skill_query"
    ),
    _PolicyKey("narrative", "play", "narration"): _ids(
        "campaign_query game_phase narrative_change narrative_query narrative_settle "
        "scene_change snapshot_change skill_query"
    ),
    _PolicyKey("narrative", "conflict", "narration"): _ids(
        "campaign_query conflict_end conflict_query narrative_query skill_query"
    ),
}


def service_for_system(system_id: str) -> str:
    try:
        return MCP_SERVICE_BY_SYSTEM[system_id]
    except KeyError as exc:
        raise RoomToolPolicyError(f"unsupported campaign system: {system_id}") from exc


def campaign_phase_and_revision(
    system_id: str,
    runtime_state: object,
) -> tuple[str, int]:
    """Normalize the three real hosted get-campaign response shapes."""

    service_for_system(system_id)
    if not isinstance(runtime_state, dict):
        raise RoomToolPolicyError("domain campaign response must be an object")
    nested = runtime_state.get("result")
    state = nested if isinstance(nested, dict) else runtime_state
    if "revision" in state and state["revision"] is not None:
        revision = state["revision"]
    else:
        revision = state.get("campaign_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RoomToolPolicyError("domain campaign did not return an authoritative revision")
    phase_value = (
        state.get("effective_game_phase")
        if state.get("effective_game_phase") is not None
        else state.get("phase")
    )
    if phase_value is None:
        phase_value = state.get("game_phase")
    phase = str(phase_value or "")
    if not any(key.system_id == system_id and key.phase == phase for key in _POLICIES):
        raise RoomToolPolicyError(
            f"domain campaign returned unsupported phase {phase!r} for {system_id}"
        )
    return phase, revision


def select_room_turn_tools(
    *,
    system_id: str,
    phase: str,
    role: str,
    task: str,
) -> tuple[str, ...]:
    """Return a deterministic exact-ID subset or fail closed.

    The Host never upgrades a player request to a narration-capable DM request.
    Unknown systems, phases, roles, tasks, empty selections, or oversized policy
    entries are configuration errors and cannot fall back to a broad catalog.
    """

    if role not in _ROLES:
        raise RoomToolPolicyError(f"unsupported campaign role: {role}")
    if task not in _TASKS:
        raise RoomToolPolicyError(f"unsupported room task: {task}")
    if task == "narration" and role not in _DM_ROLES:
        raise RoomToolPolicyError("narration requires an owner or dm role")
    service_for_system(system_id)
    operations = _POLICIES.get(_PolicyKey(system_id, phase, task), ())
    if not operations:
        raise RoomToolPolicyError(
            f"no reviewed MCP tool subset for {system_id}/{phase}/{role}/{task}"
        )
    if len(operations) > MAX_ROOM_TURN_TOOLS:
        raise RoomToolPolicyError("reviewed MCP tool subset exceeds the hard limit")
    if operations != tuple(sorted(set(operations))):
        raise RoomToolPolicyError("reviewed MCP tool subset is not deterministic")
    return operations


def policy_matrix() -> dict[tuple[str, str, str], tuple[str, ...]]:
    """Expose an immutable-copy test/documentation view of the reviewed matrix."""

    return {
        (key.system_id, key.phase, key.task): value
        for key, value in sorted(
            _POLICIES.items(),
            key=lambda item: (item[0].system_id, item[0].phase, item[0].task),
        )
    }
