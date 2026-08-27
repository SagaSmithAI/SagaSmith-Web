import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/sagasmith_service/web/room/combat-grid-state.js", import.meta.url),
  "utf8",
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const {
  MOVEMENT_INTENT_DISCLAIMER,
  clampGridCursor,
  combatantAtCell,
  decodePublicTextHeader,
  mapBounds,
  moveGridCursor,
  movementIntentSegment,
  terrainAt,
} = await import(moduleUrl);

test("normalizes canonical MCP map bounds", () => {
  assert.deepEqual(mapBounds({ bounds: { width_cells: 24, height_cells: 16 } }), {
    width: 24,
    height: 16,
  });
  assert.deepEqual(mapBounds({ width_cells: 8, height_cells: 6 }), { width: 8, height: 6 });
});

test("keyboard cursor movement remains inside the battle map", () => {
  const bounds = { width: 3, height: 2 };
  assert.deepEqual(moveGridCursor({ x: 0, y: 0 }, "ArrowLeft", bounds), { x: 0, y: 0 });
  assert.deepEqual(moveGridCursor({ x: 1, y: 0 }, "ArrowDown", bounds), { x: 1, y: 1 });
  assert.deepEqual(moveGridCursor({ x: 2, y: 1 }, "ArrowRight", bounds), { x: 2, y: 1 });
  assert.deepEqual(clampGridCursor({ x: 99, y: -5 }, bounds), { x: 2, y: 0 });
});

test("describes terrain and occupants without private character data", () => {
  const map = { blocked_cells: ["1,1"], difficult_cells: [{ x: 2, y: 1 }] };
  const actors = [{ actor_id: "hero", position: { x: 0, y: 1 }, private_hp: 3 }];
  assert.equal(terrainAt(map, { x: 1, y: 1 }), "不可通行");
  assert.equal(terrainAt(map, { x: 2, y: 1 }), "困难地形");
  assert.equal(combatantAtCell(actors, { x: 0, y: 1 }).actor_id, "hero");
});

test("movement indicator is explicitly non-authoritative", () => {
  const segment = movementIntentSegment(
    [{ actor_id: "hero", position: { x: 1, y: 2 } }],
    "hero",
    { x: 4, y: 5 },
  );
  assert.deepEqual(segment, {
    from: { x: 1, y: 2 },
    to: { x: 4, y: 5 },
    authoritative: false,
  });
  assert.match(MOVEMENT_INTENT_DISCLAIMER, /不代表合法路径.*MCP/);
});

test("decodes bounded UTF-8 public image metadata with a safe fallback", () => {
  const encoded = Buffer.from("石厅战况：Aria 当前行动。", "utf8").toString("base64url");
  assert.equal(decodePublicTextHeader(encoded, "fallback"), "石厅战况：Aria 当前行动。");
  assert.equal(decodePublicTextHeader("%%%", "fallback"), "fallback");
  assert.equal(decodePublicTextHeader("A".repeat(4097), "fallback"), "fallback");
});
