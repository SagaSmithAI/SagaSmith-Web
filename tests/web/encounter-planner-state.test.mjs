import assert from "node:assert/strict";
import test from "node:test";

import {
  buildEncounterPayload,
  createEncounterDraft,
  encounterPlacementFeedback,
  encounterValidation,
  extractEncounterTemplates,
  moveEncounterCursor,
  placeEncounterActor,
  reconcileEncounterDraft,
  seedEncounterPlacements,
  toggleEncounterActor,
} from "../../src/sagasmith_service/web/room/encounter-planner-state.js";

const actors = [
  { id: "aria", name: "Aria" },
  { id: "goblin", name: "Goblin" },
];
const templateDocument = {
  scene: {
    profile_data: {
      combat_grid_templates: [
        {
          id: "gate-ambush",
          title: "Gate Ambush",
          grid: { kind: "square", cell_ft: 5 },
          bounds: { width_cells: 6, height_cells: 4 },
          blocked_cells: [{ x: 3, y: 1 }],
          difficult_cells: [{ x: 2, y: 2 }],
          deployment_zones: [],
        },
      ],
    },
  },
};

test("extractEncounterTemplates accepts only bounded five-foot projected templates", () => {
  const templates = extractEncounterTemplates({
    profile_data: {
      combat_grid_templates: [
        ...templateDocument.scene.profile_data.combat_grid_templates,
        {
          id: "wrong-scale",
          title: "Wrong",
          grid: { kind: "square", cell_ft: 10 },
          bounds: { width_cells: 6, height_cells: 4 },
        },
        {
          id: "too-wide",
          title: "Wide",
          grid: { kind: "square", cell_ft: 5 },
          bounds: { width_cells: 201, height_cells: 4 },
        },
      ],
    },
  });
  assert.deepEqual(templates.map((template) => template.id), ["gate-ambush"]);
  assert.deepEqual(templates[0].blockedCells, [{ x: 3, y: 1 }]);
});

test("template payload is exact and excludes the mutually exclusive override", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  draft.mode = "grid";
  placeEncounterActor(draft, "aria", 0, 3);
  placeEncounterActor(draft, "goblin", 5, 0);
  assert.equal(encounterValidation(draft, templates).ready, true);
  assert.deepEqual(buildEncounterPayload(draft, templates), {
    participant_ids: ["aria", "goblin"],
    participant_config: [
      { actor_id: "aria", position: { x: 0, y: 3 } },
      { actor_id: "goblin", position: { x: 5, y: 0 } },
    ],
    positioning_mode: "grid",
    name: "遭遇战",
    battle_map_template_id: "gate-ambush",
  });
});

test("bounded override payload uses five-foot cells and excludes template authority", () => {
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 8,
    actors,
    templates: [],
  });
  draft.mode = "grid";
  draft.override = { width: 12, height: 9 };
  placeEncounterActor(draft, "aria", 0, 0);
  placeEncounterActor(draft, "goblin", 11, 8);
  const payload = buildEncounterPayload(draft, []);
  assert.equal(payload.battle_map.cell_ft, 5);
  assert.equal(payload.battle_map.width_cells, 12);
  assert.equal(payload.battle_map.height_cells, 9);
  assert.equal("battle_map_template_id" in payload, false);
  assert.match(payload.battle_map_override_reason, /临时空白网格/);
});

test("readiness reports missing, bounds, overlap, and known projected blocked cells", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  assert.deepEqual(encounterValidation(draft, templates).byActor.aria, ["尚未部署"]);
  placeEncounterActor(draft, "aria", 3, 1);
  placeEncounterActor(draft, "goblin", 3, 1);
  const overlap = encounterValidation(draft, templates);
  assert.deepEqual(overlap.byActor.aria, ["投影显示该格受阻", "与 Goblin 重叠"]);
  assert.deepEqual(overlap.byActor.goblin, ["投影显示该格受阻", "与 Aria 重叠"]);
  placeEncounterActor(draft, "goblin", 6, 0);
  assert.deepEqual(encounterValidation(draft, templates).byActor.goblin, ["坐标超出边界"]);
});

test("draft reconciliation preserves valid local placements while pruning removed actors", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  placeEncounterActor(draft, "aria", 1, 2);
  placeEncounterActor(draft, "goblin", 4, 2);
  reconcileEncounterDraft(draft, { actors: [actors[0]], templates });
  assert.deepEqual(draft.selectedIds, ["aria"]);
  assert.deepEqual(draft.placements, { aria: { x: 1, y: 2 } });
  assert.equal(draft.revisionAtDraft, 7);
});

test("reconciliation selects and seeds only newly added actors", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  draft.mode = "grid";
  placeEncounterActor(draft, "aria", 4, 3);
  toggleEncounterActor(draft, "goblin", false);

  reconcileEncounterDraft(draft, {
    actors: [...actors, { id: "wolf", name: "Wolf" }],
    templates,
  });

  assert.deepEqual(draft.selectedIds, ["aria", "wolf"]);
  assert.equal("goblin" in draft.placements, false);
  assert.deepEqual(draft.placements.aria, { x: 4, y: 3 });
  assert.deepEqual(draft.placements.wolf, { x: 0, y: 0 });
});

test("keyboard cursor remains inside the deployment board", () => {
  assert.deepEqual(moveEncounterCursor({ x: 0, y: 0 }, "ArrowLeft", { width: 6, height: 4 }), {
    x: 0,
    y: 0,
  });
  assert.deepEqual(moveEncounterCursor({ x: 2, y: 2 }, "End", { width: 6, height: 4 }), {
    x: 5,
    y: 3,
  });
});

test("deterministic prefill prefers safe projected deployment zones then row-major cells", () => {
  const templates = extractEncounterTemplates({
    profile_data: {
      combat_grid_templates: [
        {
          ...templateDocument.scene.profile_data.combat_grid_templates[0],
          deployment_zones: [
            {
              id: "party",
              cells: [
                { x: 3, y: 1 },
                { x: 5, y: 3 },
              ],
            },
          ],
        },
      ],
    },
  });
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });

  seedEncounterPlacements(draft, templates);

  assert.deepEqual(draft.placements, {
    aria: { x: 5, y: 3 },
    goblin: { x: 0, y: 0 },
  });
  assert.equal(encounterValidation(draft, templates).ready, true);
});

test("ordinary reseeding preserves user edits while source reset is deterministic", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  placeEncounterActor(draft, "aria", 4, 3);

  seedEncounterPlacements(draft, templates);
  assert.deepEqual(draft.placements.aria, { x: 4, y: 3 });
  assert.deepEqual(draft.placements.goblin, { x: 0, y: 0 });

  placeEncounterActor(draft, "aria", 3, 1);
  delete draft.placements.goblin;
  seedEncounterPlacements(draft, templates);
  assert.deepEqual(draft.placements.aria, { x: 3, y: 1 });
  assert.deepEqual(draft.placements.goblin, { x: 0, y: 0 });

  seedEncounterPlacements(draft, templates, { reset: true });
  assert.deepEqual(draft.placements, {
    aria: { x: 0, y: 0 },
    goblin: { x: 1, y: 0 },
  });
});

test("placement feedback distinguishes blocked, occupied, and outside targets", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  placeEncounterActor(draft, "aria", 0, 0);
  placeEncounterActor(draft, "goblin", 5, 0);

  assert.deepEqual(encounterPlacementFeedback(draft, templates, "aria", { x: 3, y: 1 }), {
    valid: false,
    issues: ["投影显示目标格受阻"],
  });
  assert.deepEqual(encounterPlacementFeedback(draft, templates, "aria", { x: 5, y: 0 }), {
    valid: false,
    issues: ["目标格已有 Goblin"],
  });
  assert.deepEqual(encounterPlacementFeedback(draft, templates, "aria", { x: -1, y: 0 }), {
    valid: false,
    issues: ["目标格超出边界"],
  });
});

test("removing and re-adding a participant frees then deterministically reseeds its cell", () => {
  const templates = extractEncounterTemplates(templateDocument);
  const draft = createEncounterDraft({
    campaignId: "campaign-1",
    revision: 7,
    actors,
    templates,
  });
  seedEncounterPlacements(draft, templates);
  toggleEncounterActor(draft, "aria", false);
  assert.equal("aria" in draft.placements, false);
  toggleEncounterActor(draft, "aria", true);
  seedEncounterPlacements(draft, templates);
  assert.deepEqual(draft.placements.aria, { x: 0, y: 0 });
  assert.deepEqual(draft.placements.goblin, { x: 1, y: 0 });
});
