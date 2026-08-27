import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import {
  actionContextPayload,
  activeCombat,
  actorId,
  actorName,
  battleMap,
  canControl,
  canInspect,
  cardRecord,
  characters,
  combatMode,
  combatantId,
  combatantName,
  combatants,
  currentCombatantId,
  isDm,
} from "/assets/room/model.js";
import { appendDetails, num } from "/assets/room/view.js";
import { state } from "/assets/state/store.js";

export function createCombatGridController({
  sendPanelAction,
  loadCharacterCard,
  renderCharacterSidebar,
  renderActionContext,
  refreshSuggestionValidity,
}) {
  function setGridExpanded(expanded) {
    state.gridExpanded = Boolean(
      expanded && state.panel?.phase === "combat" && combatMode() === "grid",
    );
    $("#campaign-room")?.classList.toggle("grid-expanded", state.gridExpanded);
    const shell = $("#combat-grid-shell");
    if (shell) {
      shell.classList.toggle("expanded", state.gridExpanded);
      const control = shell.querySelector("[data-grid-expand]");
      if (control) control.textContent = state.gridExpanded ? "收起" : "展开";
    }
    requestAnimationFrame(drawCombatGrid);
  }

  function renderCombatPanel() {
    const root = $("#combat-panel");
    const visible = characters();
    root.replaceChildren();
    if (state.panel?.phase !== "combat") {
      setGridExpanded(false);
      root.append(text("p", "当前没有进行中的战斗", "muted"));
      if (isDm() && state.panel?.phase === "play") {
        root.append(buildCombatStartForm(visible));
      }
      return;
    }
    const combat = activeCombat();
    const round = combat.round || combat.round_number || "—";
    const current = currentCombatantId(combat);
    root.append(
      text(
        "p",
        `第 ${round} 轮 · 当前 ${current ? combatantName(current) : "—"}`,
        "panel-title",
      ),
    );
    if (combatMode() === "grid" && battleMap()) {
      root.append(buildGridShell());
    } else {
      setGridExpanded(false);
      root.append(
        text(
          "p",
          combatMode() === "agent"
            ? "叙事空间模式：距离、视线与遮挡由 Agent 依据场景证据裁定。"
            : "当前战斗没有可用网格。",
          "small muted",
        ),
      );
      for (const item of combatants()) {
        const id = combatantId(item);
        const row = text("div", "", "combatant-row");
        row.append(
          text("span", combatantName(id)),
          text("span", item.initiative !== undefined ? `先攻 ${item.initiative}` : "", "hp-line"),
        );
        if (canControl(id)) {
          row.append(
            button("设为行动者", () => {
              state.actingActorId = id;
              renderActionContext();
            }),
          );
        }
        root.append(row);
      }
    }
    const actions = text("div", "", "panel-actions combat-grid-actions");
    if (state.actingActorId) {
      actions.append(
        button(
          "描述战斗行动",
          () => {
            const intent = prompt("攻击、施法、移动或其他战斗行动");
            if (intent) {
              sendPanelAction("combat.intent", { ...actionContextPayload(), intent });
            }
          },
          "primary",
        ),
      );
    }
    if (isDm()) {
      actions.append(
        button(
          "结束战斗",
          () => {
            const summary = prompt("战斗结果摘要", "战斗结束，队伍继续前进。");
            if (summary) sendPanelAction("combat.end", { status: "completed", summary });
          },
          "danger",
        ),
      );
    }
    root.append(actions);
    requestAnimationFrame(drawCombatGrid);
  }

  function buildCombatStartForm(visible) {
    const form = document.createElement("form");
    form.className = "combat-start-form";
    form.append(text("p", "选择参战者", "small muted"));
    for (const actor of visible) {
      const label = text("label", "", "check combatant-choice");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "participant";
      input.value = actorId(actor);
      label.append(input, document.createTextNode(actorName(actor)));
      form.append(label);
    }
    const mode = document.createElement("select");
    mode.name = "positioning_mode";
    for (const [value, label] of [
      ["agent", "Agent 叙事距离"],
      ["grid", "Grid 网格"],
    ]) {
      const option = text("option", label);
      option.value = value;
      mode.append(option);
    }
    form.append(
      mode,
      button(
        "开始战斗",
        async () => {
          const ids = [...form.querySelectorAll('input[name="participant"]:checked')].map(
            (item) => item.value,
          );
          if (!ids.length) return toast("请选择参战者");
          const payload = {
            participant_ids: ids,
            positioning_mode: mode.value,
            name: prompt("战斗名称", "遭遇战") || "遭遇战",
          };
          if (mode.value === "grid") {
            const width = Number(prompt("网格宽度（格）", "20"));
            const height = Number(prompt("网格高度（格）", "14"));
            if (
              !Number.isInteger(width) ||
              !Number.isInteger(height) ||
              width < 1 ||
              height < 1 ||
              width > 200 ||
              height > 200
            ) {
              return toast("网格尺寸必须是 1 到 200 的整数");
            }
            payload.battle_map = {
              width_cells: width,
              height_cells: height,
              blocked_cells: [],
              difficult_cells: [],
            };
            payload.battle_map_override_reason = "由 DM 在 Web 战斗面板创建的临时空白网格";
            payload.participant_config = [];
            for (let index = 0; index < ids.length; index++) {
              const id = ids[index];
              const raw = prompt(`${combatantName(id)} 的起始坐标 x,y`, `${index},0`);
              if (raw === null) return;
              const [x, y] = raw.split(",").map(Number);
              if (
                !Number.isInteger(x) ||
                !Number.isInteger(y) ||
                x < 0 ||
                y < 0 ||
                x >= width ||
                y >= height
              ) {
                return toast(`${combatantName(id)} 的坐标无效`);
              }
              payload.participant_config.push({ actor_id: id, position: { x, y } });
            }
          }
          await sendPanelAction("combat.start", payload);
        },
        "primary",
      ),
    );
    return form;
  }

  function buildGridShell() {
    const combat = activeCombat();
    const shell = text("section", "", "grid-shell");
    shell.id = "combat-grid-shell";
    const head = text("header", "", "grid-head");
    head.append(
      text("strong", battleMap()?.name || combat.name || "战斗网格"),
      text("span", `R${combat.round || combat.round_number || "—"}`, "small muted"),
    );
    const expand = button(state.gridExpanded ? "收起" : "展开", () =>
      setGridExpanded(!state.gridExpanded),
    );
    expand.dataset.gridExpand = "true";
    head.append(expand);
    const wrap = text("div", "", "grid-canvas-wrap");
    const canvas = document.createElement("canvas");
    const tooltip = text("div", "", "grid-tooltip");
    canvas.id = "combat-grid";
    canvas.className = "combat-grid";
    canvas.setAttribute("aria-label", "战斗网格");
    tooltip.hidden = true;
    wrap.append(canvas, tooltip);
    const initiative = text("div", "", "initiative-strip");
    const current = currentCombatantId(combat);
    for (const item of [...combatants()].sort(
      (a, b) => Number(b.initiative || 0) - Number(a.initiative || 0),
    )) {
      const id = combatantId(item);
      const token = button(
        `${item.initiative ?? "—"} ${combatantName(id)}`,
        () => focusCombatant(id),
        "initiative-token",
      );
      token.classList.toggle("current", id === current);
      token.classList.toggle("acting", id === state.actingActorId);
      initiative.append(token);
    }
    shell.append(
      head,
      wrap,
      initiative,
      text(
        "div",
        "点击己方角色设为行动者；点击目标或空格建立聊天行动上下文。",
        "grid-legend",
      ),
    );
    shell.classList.toggle("expanded", state.gridExpanded);
    canvas.onpointermove = (event) => gridPointerMove(event, canvas, tooltip);
    canvas.onpointerleave = () => {
      tooltip.hidden = true;
    };
    canvas.onclick = (event) => gridClick(event, canvas);
    return shell;
  }

  async function focusCombatant(id) {
    if (canControl(id)) state.actingActorId = id;
    else state.selectedTargetId = id;
    if (canInspect(id)) {
      state.inspectedActorId = id;
      await loadCharacterCard(id, { quiet: true });
      renderCharacterSidebar();
    }
    renderActionContext();
    refreshSuggestionValidity();
    drawCombatGrid();
  }

  function mapBounds(map) {
    const bounds = map?.bounds || {};
    return {
      width: Number(
        bounds.width_cells || bounds.width || bounds.columns || map?.width_cells || map?.width || 20,
      ),
      height: Number(
        bounds.height_cells || bounds.height || bounds.rows || map?.height_cells || map?.height || 14,
      ),
    };
  }

  function cellKey(cell) {
    if (typeof cell === "string") return cell;
    if (Array.isArray(cell)) return `${cell[0]},${cell[1]}`;
    return `${cell?.x},${cell?.y}`;
  }

  function gridMetrics(canvas) {
    const map = battleMap();
    const bounds = mapBounds(map);
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (
      canvas.width !== Math.round(rect.width * dpr) ||
      canvas.height !== Math.round(rect.height * dpr)
    ) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    }
    const cell = Math.max(
      8,
      Math.min(canvas.width / bounds.width, canvas.height / bounds.height),
    );
    const offsetX = (canvas.width - cell * bounds.width) / 2;
    const offsetY = (canvas.height - cell * bounds.height) / 2;
    return { bounds, dpr, cell, offsetX, offsetY };
  }

  function drawCombatGrid() {
    const canvas = $("#combat-grid");
    if (!canvas || combatMode() !== "grid") return;
    const map = battleMap();
    if (!map) return;
    const context = canvas.getContext("2d");
    const metrics = gridMetrics(canvas);
    const blocked = new Set((map.blocked_cells || map.blocked || []).map(cellKey));
    const difficult = new Set(
      (map.difficult_terrain || map.difficult_cells || []).map(cellKey),
    );
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#0b0f0d";
    context.fillRect(0, 0, canvas.width, canvas.height);
    for (let y = 0; y < metrics.bounds.height; y++) {
      for (let x = 0; x < metrics.bounds.width; x++) {
        const key = `${x},${y}`;
        if (blocked.has(key)) {
          context.fillStyle = "#34302e";
          context.fillRect(
            metrics.offsetX + x * metrics.cell,
            metrics.offsetY + y * metrics.cell,
            metrics.cell,
            metrics.cell,
          );
        } else if (difficult.has(key)) {
          context.fillStyle = "#253529";
          context.fillRect(
            metrics.offsetX + x * metrics.cell,
            metrics.offsetY + y * metrics.cell,
            metrics.cell,
            metrics.cell,
          );
        }
      }
    }
    context.strokeStyle = "#29302c";
    context.lineWidth = Math.max(1, metrics.dpr * 0.5);
    for (let x = 0; x <= metrics.bounds.width; x++) {
      context.beginPath();
      context.moveTo(metrics.offsetX + x * metrics.cell, metrics.offsetY);
      context.lineTo(
        metrics.offsetX + x * metrics.cell,
        metrics.offsetY + metrics.bounds.height * metrics.cell,
      );
      context.stroke();
    }
    for (let y = 0; y <= metrics.bounds.height; y++) {
      context.beginPath();
      context.moveTo(metrics.offsetX, metrics.offsetY + y * metrics.cell);
      context.lineTo(
        metrics.offsetX + metrics.bounds.width * metrics.cell,
        metrics.offsetY + y * metrics.cell,
      );
      context.stroke();
    }
    if (state.gridDestination) {
      context.fillStyle = "#d9ad5b55";
      context.fillRect(
        metrics.offsetX + state.gridDestination.x * metrics.cell,
        metrics.offsetY + state.gridDestination.y * metrics.cell,
        metrics.cell,
        metrics.cell,
      );
    }
    const current = currentCombatantId();
    for (const item of combatants()) {
      const position = item.position || item.coordinates;
      if (!position) continue;
      const id = combatantId(item);
      const centerX = metrics.offsetX + (Number(position.x) + 0.5) * metrics.cell;
      const centerY = metrics.offsetY + (Number(position.y) + 0.5) * metrics.cell;
      const radius = Math.max(6, metrics.cell * 0.34);
      const owned = canControl(id);
      const selected = id === state.selectedTargetId;
      context.beginPath();
      context.arc(centerX, centerY, radius, 0, Math.PI * 2);
      context.fillStyle = owned
        ? "#658d70"
        : item.disposition === "hostile"
          ? "#98574f"
          : "#65707f";
      context.fill();
      context.lineWidth = selected ? 4 : current === id ? 3 : 1;
      context.strokeStyle = selected ? "#e37765" : current === id ? "#d9ad5b" : "#d9ddd8";
      context.stroke();
      context.fillStyle = "#fff";
      context.font = `600 ${Math.max(8, Math.min(13, metrics.cell * 0.24))}px system-ui`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(combatantName(id).slice(0, 2), centerX, centerY);
      const record = cardRecord(id)?.actor;
      const hp = record?.derived?.hit_points || record?.sheet?.combat?.hp;
      if (hp && Number(hp.max ?? hp.maximum) > 0) {
        const ratio = Math.max(
          0,
          Math.min(
            1,
            Number((hp.value ?? hp.current) ?? 0) / Number(hp.max ?? hp.maximum),
          ),
        );
        context.fillStyle = "#321918";
        context.fillRect(centerX - radius, centerY + radius + 3, radius * 2, 3 * metrics.dpr);
        context.fillStyle = "#a85e51";
        context.fillRect(
          centerX - radius,
          centerY + radius + 3,
          radius * 2 * ratio,
          3 * metrics.dpr,
        );
      }
    }
  }

  function gridCell(event, canvas) {
    const metrics = gridMetrics(canvas);
    const rect = canvas.getBoundingClientRect();
    const x = Math.floor(
      ((event.clientX - rect.left) * metrics.dpr - metrics.offsetX) / metrics.cell,
    );
    const y = Math.floor(
      ((event.clientY - rect.top) * metrics.dpr - metrics.offsetY) / metrics.cell,
    );
    return x >= 0 && y >= 0 && x < metrics.bounds.width && y < metrics.bounds.height
      ? { x, y }
      : null;
  }

  function combatantAt(cell) {
    return combatants().find((item) => {
      const position = item.position || item.coordinates;
      return position && Number(position.x) === cell?.x && Number(position.y) === cell?.y;
    });
  }

  function gridPointerMove(event, canvas, tooltip) {
    const cell = gridCell(event, canvas);
    if (!cell) {
      tooltip.hidden = true;
      return;
    }
    const item = combatantAt(cell);
    const map = battleMap();
    const terrain = (map.blocked_cells || map.blocked || []).some(
      (candidate) => cellKey(candidate) === cellKey(cell),
    )
      ? "不可通行"
      : (map.difficult_terrain || map.difficult_cells || []).some(
            (candidate) => cellKey(candidate) === cellKey(cell),
          )
        ? "困难地形"
        : "普通地形";
    tooltip.replaceChildren(
      text(
        "strong",
        item ? combatantName(combatantId(item)) : `坐标 ${cell.x}, ${cell.y}`,
      ),
    );
    if (item) {
      const id = combatantId(item);
      const actor = cardRecord(id)?.actor;
      const hp = actor?.derived?.hit_points || actor?.sheet?.combat?.hp;
      appendDetails(tooltip, [
        ["先攻", item.initiative],
        ["位置", `${cell.x}, ${cell.y}`],
        ["HP", hp ? `${num(hp.value ?? hp.current)}/${num(hp.max ?? hp.maximum)}` : undefined],
        ["AC", actor?.derived?.armor_class],
        [
          "状态",
          (item.conditions || [])
            .map((condition) =>
              typeof condition === "string" ? condition : condition.name || condition.id,
            )
            .join("、"),
        ],
        ["回合预算", item.turn_budget ? JSON.stringify(item.turn_budget) : undefined],
      ]);
    } else {
      tooltip.append(text("p", terrain, "small muted"));
    }
    const wrap = canvas.parentElement;
    const rect = wrap.getBoundingClientRect();
    tooltip.style.left = `${Math.min(event.clientX - rect.left + 12, rect.width - 250)}px`;
    tooltip.style.top = `${Math.min(event.clientY - rect.top + 12, rect.height - 140)}px`;
    tooltip.hidden = false;
  }

  async function gridClick(event, canvas) {
    const cell = gridCell(event, canvas);
    if (!cell) return;
    const item = combatantAt(cell);
    if (item) {
      await focusCombatant(combatantId(item));
      return;
    }
    if (state.actingActorId) {
      state.gridDestination = cell;
      state.selectedTargetId = null;
      renderActionContext();
      drawCombatGrid();
    }
  }

  function initialize() {
    window.addEventListener("resize", () => requestAnimationFrame(drawCombatGrid));
  }

  return {
    drawCombatGrid,
    initialize,
    renderCombatPanel,
    setGridExpanded,
  };
}
