import { state } from "/assets/state/store.js";

export function asList(value, ...keys) {
  if (Array.isArray(value)) return value;
  for (const key of keys) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  return [];
}

export function actorName(actor) {
  return actor.name || actor.display_name || actor.sheet?.name || actor.id || "未知角色";
}

export function actorHp(actor) {
  const hp = actor.sheet?.combat?.hp || actor.combat?.hp || {};
  return {
    value: hp.value ?? hp.current ?? "—",
    maximum: hp.maximum ?? hp.max ?? "—",
  };
}

export function isDm() {
  return ["owner", "dm"].includes(state.membership?.role);
}

export function characters() {
  return asList(state.panel?.characters, "characters", "items");
}

export function actorId(actor) {
  return actor?.id || actor?.character_id || actor?.actor_id || "";
}

export function actorBinding(id) {
  return (state.panel?.actor_bindings || []).find(
    (binding) =>
      binding.actor_id === id && binding.user_id === state.user.id && binding.status !== "revoked",
  );
}

export function canInspect(id) {
  const binding = actorBinding(id);
  return isDm() || Boolean(binding?.can_view_private);
}

export function canControl(id) {
  const binding = actorBinding(id);
  return isDm() || Boolean(binding?.can_control);
}

export function cardRecord(id) {
  return state.characterCards.get(id) || null;
}

export function characterActor(id = state.inspectedActorId) {
  return cardRecord(id)?.actor || characters().find((actor) => actorId(actor) === id) || null;
}

export function actionContextPayload() {
  const payload = {};
  if (state.actingActorId) payload.actor_id = state.actingActorId;
  if (state.selectedTargetId) payload.target_id = state.selectedTargetId;
  if (state.gridDestination) {
    payload.grid = { destination: state.gridDestination, positioning_mode: "grid" };
  }
  return payload;
}

export function activeCombat() {
  return state.panel?.combat?.combat || state.panel?.combat || {};
}

export function combatants() {
  return asList(activeCombat(), "combatants", "participants");
}

export function combatantId(item) {
  return item?.actor_id || item?.id || item?.character_id || "";
}

export function combatantName(id) {
  const item = combatants().find((candidate) => combatantId(candidate) === id);
  const actor = characters().find((candidate) => actorId(candidate) === id);
  return actorName(actor || item || { id });
}

export function currentCombatantId(combat = activeCombat()) {
  const direct =
    combatantId(combat.current_turn) ||
    combat.turn_actor_id ||
    combat.active_actor_id ||
    combat.current_actor_id;
  if (direct) return String(direct);
  const index = Number(combat.turn_index);
  return Number.isInteger(index) ? combatantId(combatants()[index]) : "";
}

export function battleMap() {
  const combat = activeCombat();
  return combat.battle_map || combat.map || combat.grid || null;
}

export function combatMode() {
  const combat = activeCombat();
  return combat.positioning_mode || combat.spatial_mode || combat.mode;
}
