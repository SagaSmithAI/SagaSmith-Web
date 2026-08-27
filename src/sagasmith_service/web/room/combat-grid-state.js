export const MOVEMENT_INTENT_DISCLAIMER = "移动意图连线，不代表合法路径；最终由 MCP 校验。";

export function decodePublicTextHeader(value, fallback = "") {
  if (!value || typeof value !== "string" || value.length > 4096) return fallback;
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
    const decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes).trim();
    return decoded || fallback;
  } catch {
    return fallback;
  }
}

function positiveInteger(value, fallback) {
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric > 0 ? Math.min(numeric, 200) : fallback;
}

export function mapBounds(map) {
  const bounds = map?.bounds || {};
  return {
    width: positiveInteger(
      bounds.width_cells ?? bounds.width ?? bounds.columns ?? map?.width_cells ?? map?.width,
      20,
    ),
    height: positiveInteger(
      bounds.height_cells ?? bounds.height ?? bounds.rows ?? map?.height_cells ?? map?.height,
      14,
    ),
  };
}

export function cellKey(cell) {
  if (typeof cell === "string") return cell;
  if (Array.isArray(cell)) return `${cell[0]},${cell[1]}`;
  return `${cell?.x},${cell?.y}`;
}

export function clampGridCursor(cursor, bounds) {
  return {
    x: Math.max(0, Math.min(bounds.width - 1, Number(cursor?.x) || 0)),
    y: Math.max(0, Math.min(bounds.height - 1, Number(cursor?.y) || 0)),
  };
}

export function moveGridCursor(cursor, key, bounds) {
  const next = { ...clampGridCursor(cursor, bounds) };
  if (key === "ArrowLeft") next.x -= 1;
  if (key === "ArrowRight") next.x += 1;
  if (key === "ArrowUp") next.y -= 1;
  if (key === "ArrowDown") next.y += 1;
  if (key === "Home") next.x = 0;
  if (key === "End") next.x = bounds.width - 1;
  return clampGridCursor(next, bounds);
}

export function terrainAt(map, cell) {
  const key = cellKey(cell);
  if ((map?.blocked_cells || map?.blocked || []).some((item) => cellKey(item) === key)) {
    return "不可通行";
  }
  if (
    (map?.difficult_terrain || map?.difficult_cells || []).some(
      (item) => cellKey(item) === key,
    )
  ) {
    return "困难地形";
  }
  return "普通地形";
}

export function combatantAtCell(items, cell) {
  return items.find((item) => {
    const position = item.position || item.coordinates;
    return position && Number(position.x) === cell?.x && Number(position.y) === cell?.y;
  });
}

export function movementIntentSegment(items, actorId, destination) {
  if (!actorId || !destination) return null;
  const actor = items.find(
    (item) => String(item.actor_id || item.id || item.character_id || "") === String(actorId),
  );
  const origin = actor?.position || actor?.coordinates;
  if (!origin) return null;
  return {
    from: { x: Number(origin.x), y: Number(origin.y) },
    to: { x: Number(destination.x), y: Number(destination.y) },
    authoritative: false,
  };
}
