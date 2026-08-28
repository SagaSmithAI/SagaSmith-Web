const MAX_MAP_CELLS = 200;

function integer(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}

function cells(value) {
  return Array.isArray(value)
    ? value
        .map((cell) => ({ x: integer(cell?.x), y: integer(cell?.y) }))
        .filter((cell) => cell.x !== null && cell.y !== null)
    : [];
}

function templateCandidates(currentModule) {
  const roots = [currentModule, currentModule?.scene].filter(Boolean);
  const result = [];
  for (const root of roots) {
    for (const source of [root, root?.profile_data, root?.metadata?.profile_data]) {
      const templates = source?.combat_grid_templates;
      if (Array.isArray(templates)) result.push(...templates);
    }
  }
  return result;
}

export function extractEncounterTemplates(currentModule) {
  const byId = new Map();
  for (const value of templateCandidates(currentModule)) {
    const id = String(value?.id || "").trim();
    const width = integer(value?.bounds?.width_cells ?? value?.width_cells);
    const height = integer(value?.bounds?.height_cells ?? value?.height_cells);
    const cellFt = integer(value?.grid?.cell_ft ?? value?.cell_ft ?? 5);
    if (
      !id ||
      width === null ||
      height === null ||
      width < 1 ||
      height < 1 ||
      width > MAX_MAP_CELLS ||
      height > MAX_MAP_CELLS ||
      cellFt !== 5
    ) {
      continue;
    }
    byId.set(id, {
      id,
      title: String(value.title || value.name || id),
      width,
      height,
      cellFt,
      blockedCells: cells(value.blocked_cells || value.blocked),
      difficultCells: cells(value.difficult_cells || value.difficult_terrain),
      deploymentZones: Array.isArray(value.deployment_zones)
        ? value.deployment_zones
            .map((zone) => ({
              id: String(zone?.id || ""),
              cells: cells(zone?.cells),
            }))
            .filter((zone) => zone.id && zone.cells.length)
            .sort((left, right) => left.id.localeCompare(right.id))
        : [],
    });
  }
  return [...byId.values()].sort((left, right) => left.title.localeCompare(right.title));
}

export function createEncounterDraft({ campaignId, revision, actors, templates }) {
  const roster = actors
    .map((actor) => ({ id: String(actor.id), name: String(actor.name || actor.id) }))
    .filter((actor) => actor.id);
  const firstTemplate = templates[0] || null;
  return {
    campaignId: String(campaignId),
    revisionAtDraft: Number(revision || 0),
    name: "遭遇战",
    mode: "agent",
    plannerOpen: false,
    reviewOpen: false,
    sourceKind: firstTemplate ? "template" : "override",
    templateId: firstTemplate?.id || "",
    override: { width: 20, height: 14 },
    actors: roster,
    selectedIds: roster.map((actor) => actor.id),
    placements: {},
    activeActorId: roster[0]?.id || "",
    cursor: { x: 0, y: 0 },
    submitError: "",
  };
}

export function reconcileEncounterDraft(draft, { actors, templates }) {
  const previousActorIds = new Set(draft.actors.map((actor) => actor.id));
  const roster = actors
    .map((actor) => ({ id: String(actor.id), name: String(actor.name || actor.id) }))
    .filter((actor) => actor.id);
  const ids = new Set(roster.map((actor) => actor.id));
  const addedIds = roster
    .map((actor) => actor.id)
    .filter((actorId) => !previousActorIds.has(actorId));
  draft.actors = roster;
  const selected = new Set(draft.selectedIds.filter((id) => ids.has(id)));
  for (const actorId of addedIds) selected.add(actorId);
  draft.selectedIds = roster.map((actor) => actor.id).filter((actorId) => selected.has(actorId));
  draft.placements = Object.fromEntries(
    Object.entries(draft.placements).filter(([id]) => ids.has(id)),
  );
  if (!ids.has(draft.activeActorId)) draft.activeActorId = draft.selectedIds[0] || "";
  let sourceChanged = false;
  if (draft.sourceKind === "template" && !templates.some((item) => item.id === draft.templateId)) {
    draft.templateId = templates[0]?.id || "";
    if (!draft.templateId) draft.sourceKind = "override";
    sourceChanged = true;
  }
  if (sourceChanged) seedEncounterPlacements(draft, templates, { reset: true });
  else if (addedIds.length && draft.mode === "grid") {
    seedEncounterPlacements(draft, templates);
  }
  return draft;
}

export function mapForEncounter(draft, templates) {
  if (draft.sourceKind === "template") {
    const template = templates.find((item) => item.id === draft.templateId);
    if (!template) return null;
    return {
      width: template.width,
      height: template.height,
      cellFt: 5,
      blockedCells: template.blockedCells,
      difficultCells: template.difficultCells,
      deploymentZones: template.deploymentZones,
      title: template.title,
    };
  }
  return {
    width: integer(draft.override.width),
    height: integer(draft.override.height),
    cellFt: 5,
    blockedCells: [],
    difficultCells: [],
    deploymentZones: [],
    title: "临时空白网格",
  };
}

export function encounterValidation(draft, templates) {
  const map = mapForEncounter(draft, templates);
  const global = [];
  const byActor = {};
  const selected = new Set(draft.selectedIds);
  if (!draft.name.trim()) global.push("需要战斗名称");
  if (!selected.size) global.push("至少选择一名参战者");
  if (!map) global.push("所选模块地图模板已不可用");
  if (
    map &&
    (!Number.isInteger(map.width) ||
      !Number.isInteger(map.height) ||
      map.width < 1 ||
      map.height < 1 ||
      map.width > MAX_MAP_CELLS ||
      map.height > MAX_MAP_CELLS)
  ) {
    global.push("地图宽高必须是 1 到 200 格的整数");
  }
  const occupied = new Map();
  const blocked = new Set((map?.blockedCells || []).map((cell) => `${cell.x},${cell.y}`));
  for (const actor of draft.actors.filter((item) => selected.has(item.id))) {
    const errors = [];
    const position = draft.placements[actor.id];
    const x = integer(position?.x);
    const y = integer(position?.y);
    if (x === null || y === null) {
      errors.push("尚未部署");
    } else if (
      !map ||
      !Number.isInteger(map.width) ||
      !Number.isInteger(map.height) ||
      x < 0 ||
      y < 0 ||
      x >= map.width ||
      y >= map.height
    ) {
      errors.push("坐标超出边界");
    } else {
      const key = `${x},${y}`;
      if (blocked.has(key)) errors.push("投影显示该格受阻");
      const previous = occupied.get(key);
      if (previous) {
        errors.push(`与 ${previous.name} 重叠`);
        const previousErrors = byActor[previous.id] || [];
        const message = `与 ${actor.name} 重叠`;
        if (!previousErrors.includes(message)) previousErrors.push(message);
        byActor[previous.id] = previousErrors;
      } else {
        occupied.set(key, actor);
      }
    }
    byActor[actor.id] = [...(byActor[actor.id] || []), ...errors];
  }
  return {
    ready: global.length === 0 && Object.values(byActor).every((errors) => !errors.length),
    global,
    byActor,
    map,
  };
}

export function placeEncounterActor(draft, actorId, x, y) {
  draft.placements[String(actorId)] = { x: integer(x), y: integer(y) };
  draft.activeActorId = String(actorId);
  draft.submitError = "";
  return draft;
}

function cellKey(cell) {
  return `${cell.x},${cell.y}`;
}

export function encounterPlacementFeedback(draft, templates, actorId, position) {
  const map = mapForEncounter(draft, templates);
  const x = integer(position?.x);
  const y = integer(position?.y);
  if (
    !map ||
    x === null ||
    y === null ||
    !Number.isInteger(map.width) ||
    !Number.isInteger(map.height) ||
    x < 0 ||
    y < 0 ||
    x >= map.width ||
    y >= map.height
  ) {
    return { valid: false, issues: ["目标格超出边界"] };
  }
  const issues = [];
  const key = `${x},${y}`;
  if (new Set((map.blockedCells || []).map(cellKey)).has(key)) {
    issues.push("投影显示目标格受阻");
  }
  const occupantId = draft.selectedIds.find((candidate) => {
    if (candidate === String(actorId)) return false;
    const candidatePosition = draft.placements[candidate];
    return candidatePosition?.x === x && candidatePosition?.y === y;
  });
  if (occupantId) {
    const occupant = draft.actors.find((actor) => actor.id === occupantId);
    issues.push(`目标格已有 ${occupant?.name || occupantId}`);
  }
  return { valid: issues.length === 0, issues };
}

export function seedEncounterPlacements(draft, templates, { reset = false } = {}) {
  const map = mapForEncounter(draft, templates);
  if (
    !map ||
    !Number.isInteger(map.width) ||
    !Number.isInteger(map.height) ||
    map.width < 1 ||
    map.height < 1 ||
    map.width > MAX_MAP_CELLS ||
    map.height > MAX_MAP_CELLS
  ) {
    return draft;
  }
  if (reset) draft.placements = {};
  const blocked = new Set((map.blockedCells || []).map(cellKey));
  const occupied = new Set();
  const preserved = new Set();
  for (const actorId of draft.selectedIds) {
    const position = draft.placements[actorId];
    if (
      !reset &&
      Number.isInteger(position?.x) &&
      Number.isInteger(position?.y)
    ) {
      preserved.add(actorId);
      if (
        position.x >= 0 &&
        position.y >= 0 &&
        position.x < map.width &&
        position.y < map.height
      ) {
        occupied.add(cellKey(position));
      }
      continue;
    }
    if (
      Number.isInteger(position?.x) &&
      Number.isInteger(position?.y) &&
      position.x >= 0 &&
      position.y >= 0 &&
      position.x < map.width &&
      position.y < map.height &&
      !blocked.has(cellKey(position)) &&
      !occupied.has(cellKey(position))
    ) {
      occupied.add(cellKey(position));
      preserved.add(actorId);
    }
  }
  const zoneCells = (map.deploymentZones || []).flatMap((zone) => zone.cells || []);
  const candidates = [];
  const seen = new Set();
  for (const cell of zoneCells) {
    const key = cellKey(cell);
    if (
      !seen.has(key) &&
      cell.x >= 0 &&
      cell.y >= 0 &&
      cell.x < map.width &&
      cell.y < map.height &&
      !blocked.has(key)
    ) {
      candidates.push(cell);
      seen.add(key);
    }
  }
  for (let y = 0; y < map.height; y += 1) {
    for (let x = 0; x < map.width; x += 1) {
      const cell = { x, y };
      const key = cellKey(cell);
      if (!seen.has(key) && !blocked.has(key)) {
        candidates.push(cell);
        seen.add(key);
      }
    }
  }
  for (const actorId of draft.selectedIds) {
    if (preserved.has(actorId)) continue;
    const next = candidates.find((cell) => !occupied.has(cellKey(cell)));
    if (!next) break;
    draft.placements[actorId] = { ...next };
    occupied.add(cellKey(next));
  }
  draft.submitError = "";
  return draft;
}

export function toggleEncounterActor(draft, actorId, selected) {
  const id = String(actorId);
  const ids = new Set(draft.selectedIds);
  if (selected) ids.add(id);
  else {
    ids.delete(id);
    delete draft.placements[id];
  }
  draft.selectedIds = draft.actors.map((actor) => actor.id).filter((candidate) => ids.has(candidate));
  if (!draft.selectedIds.includes(draft.activeActorId)) {
    draft.activeActorId = draft.selectedIds[0] || "";
  }
  draft.submitError = "";
  return draft;
}

export function buildEncounterPayload(draft, templates) {
  const validation = encounterValidation(draft, templates);
  if (!validation.ready) throw new Error("部署草稿仍有待处理项");
  const payload = {
    participant_ids: [...draft.selectedIds],
    participant_config: draft.selectedIds.map((actorId) => ({
      actor_id: actorId,
      position: { ...draft.placements[actorId] },
    })),
    positioning_mode: "grid",
    name: draft.name.trim(),
  };
  if (draft.sourceKind === "template") {
    payload.battle_map_template_id = draft.templateId;
  } else {
    payload.battle_map = {
      width_cells: validation.map.width,
      height_cells: validation.map.height,
      cell_ft: 5,
      blocked_cells: [],
      difficult_cells: [],
    };
    payload.battle_map_override_reason = "由 DM 在 SagaSmith Web 遭遇规划台创建的临时空白网格";
  }
  return payload;
}

export function moveEncounterCursor(cursor, key, bounds) {
  const next = { x: integer(cursor?.x) ?? 0, y: integer(cursor?.y) ?? 0 };
  if (key === "ArrowLeft") next.x -= 1;
  if (key === "ArrowRight") next.x += 1;
  if (key === "ArrowUp") next.y -= 1;
  if (key === "ArrowDown") next.y += 1;
  if (key === "Home") return { x: 0, y: 0 };
  if (key === "End") {
    return { x: Math.max(0, bounds.width - 1), y: Math.max(0, bounds.height - 1) };
  }
  return {
    x: Math.max(0, Math.min(Math.max(0, bounds.width - 1), next.x)),
    y: Math.max(0, Math.min(Math.max(0, bounds.height - 1), next.y)),
  };
}
