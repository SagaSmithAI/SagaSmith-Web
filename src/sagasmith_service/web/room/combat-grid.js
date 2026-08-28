import { apiBlobResponse } from "/assets/api/client.js";
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
import {
  MOVEMENT_INTENT_DISCLAIMER,
  cellKey,
  clampGridCursor,
  combatantAtCell,
  decodePublicTextHeader,
  mapBounds,
  moveGridCursor,
  movementIntentSegment,
  terrainAt,
} from "/assets/room/combat-grid-state.js";
import {
  buildEncounterPayload,
  createEncounterDraft,
  encounterValidation,
  encounterPlacementFeedback,
  extractEncounterTemplates,
  mapForEncounter,
  moveEncounterCursor,
  placeEncounterActor,
  reconcileEncounterDraft,
  seedEncounterPlacements,
  toggleEncounterActor,
} from "/assets/room/encounter-planner-state.js";

const gridTexture = new Image();
gridTexture.decoding = "async";
gridTexture.src = "/sagasmith-grid-texture.webp";

export function createCombatGridController({
  sendPanelAction,
  loadCharacterCard,
  renderCharacterSidebar,
  renderActionContext,
  refreshSuggestionValidity,
}) {
  let combatSnapshotBlob = null;
  let combatSnapshotUrl = null;
  let combatSnapshotCaption = "全队公开战况图。";

  function releaseCombatSnapshot() {
    if (combatSnapshotUrl) URL.revokeObjectURL(combatSnapshotUrl);
    combatSnapshotBlob = null;
    combatSnapshotUrl = null;
    combatSnapshotCaption = "全队公开战况图。";
  }

  function setGridExpanded(expanded) {
    state.gridExpanded = Boolean(
      expanded && state.panel?.phase === "combat" && combatMode() === "grid",
    );
    $("#campaign-room")?.classList.toggle("grid-expanded", state.gridExpanded);
    const shell = $("#combat-grid-shell");
    if (shell) {
      shell.classList.toggle("expanded", state.gridExpanded);
      const control = shell.querySelector("[data-grid-expand]");
      if (control) {
        control.textContent = state.gridExpanded ? "收起" : "展开";
        control.setAttribute("aria-expanded", String(state.gridExpanded));
      }
    }
    requestAnimationFrame(drawCombatGrid);
  }

  function renderCombatPanel() {
    const root = $("#combat-panel");
    const visible = characters();
    releaseCombatSnapshot();
    root.replaceChildren();
    if (state.panel?.phase !== "combat") {
      state.gridCursor = null;
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
      state.gridCursor = null;
      state.gridDestination = null;
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
    form.className = "combat-start-form combat-command-table";
    form.onsubmit = (event) => event.preventDefault();
    const templates = extractEncounterTemplates(state.panel?.current_module);
    const actors = visible.map((actor) => ({ id: actorId(actor), name: actorName(actor) }));
    if (!state.encounterDraft || state.encounterDraft.campaignId !== state.campaign.id) {
      state.encounterDraft = createEncounterDraft({
        campaignId: state.campaign.id,
        revision: state.panel?.revision,
        actors,
        templates,
      });
    } else {
      reconcileEncounterDraft(state.encounterDraft, { actors, templates });
    }
    const draft = state.encounterDraft;

    const render = () => {
      form.replaceChildren();
      form.classList.toggle(
        "planner-open",
        draft.mode === "grid" && draft.plannerOpen,
      );
      const header = text("header", "", "encounter-command-head");
      const title = text("div", "", "encounter-command-title");
      title.append(
        text("span", "ENCOUNTER // DEPLOYMENT", "encounter-kicker"),
        text("strong", "遭遇部署台"),
        text("span", `草稿基于 MCP revision ${draft.revisionAtDraft}`, "small muted"),
      );
      const nameLabel = text("label", "", "encounter-name");
      nameLabel.append(text("span", "战斗名称", "small muted"));
      const name = document.createElement("input");
      name.name = "encounter_name";
      name.maxLength = 160;
      name.value = draft.name;
      name.oninput = () => {
        draft.name = name.value;
        draft.submitError = "";
      };
      nameLabel.append(name);
      const mode = document.createElement("select");
      mode.name = "positioning_mode";
      mode.setAttribute("aria-label", "定位模式");
      for (const [value, label] of [
        ["agent", "Agent 叙事距离"],
        ["grid", "Grid 战术部署"],
      ]) {
        const option = text("option", label);
        option.value = value;
        option.selected = draft.mode === value;
        mode.append(option);
      }
      mode.onchange = () => {
        draft.mode = mode.value;
        if (draft.mode === "grid") {
          draft.plannerOpen = true;
          seedEncounterPlacements(draft, templates);
        }
        draft.submitError = "";
        render();
      };
      header.append(title, nameLabel, mode);
      form.append(header);

      if (draft.mode === "agent") {
        form.append(buildAgentEncounterStart(draft, render));
        return;
      }
      if (!draft.plannerOpen) {
        const resume = text("div", "", "encounter-resume");
        resume.append(
          text("strong", "Grid 部署草稿已保留"),
          text(
            "p",
            `${draft.selectedIds.length} 名参战者 · ${Object.keys(draft.placements).length} 个坐标`,
            "small muted",
          ),
          button("继续部署", () => {
            draft.plannerOpen = true;
            render();
          }, "primary"),
        );
        form.append(resume);
        return;
      }
      form.append(buildGridEncounterPlanner(draft, templates, render));
    };
    render();
    return form;
  }

  function buildAgentEncounterStart(draft, rerender) {
    const section = text("section", "", "encounter-agent-start");
    section.append(
      text("p", "选择参战者；定位、距离与遮挡由 Agent 依据场景证据交给 MCP 裁定。", "small muted"),
    );
    const roster = text("div", "", "encounter-agent-roster");
    for (const actor of draft.actors) {
      const label = text("label", "", "check combatant-choice");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = draft.selectedIds.includes(actor.id);
      input.onchange = () => {
        toggleEncounterActor(draft, actor.id, input.checked);
      };
      label.append(input, document.createTextNode(actor.name));
      roster.append(label);
    }
    const start = button(
      "开始叙事战斗",
      async () => {
        if (!draft.selectedIds.length) return toast("请选择参战者");
        if (!draft.name.trim()) return toast("请输入战斗名称");
        draft.submitError = "";
        try {
          await sendPanelAction("combat.start", {
            participant_ids: [...draft.selectedIds],
            positioning_mode: "agent",
            name: draft.name.trim(),
          });
          state.encounterDraft = null;
        } catch (error) {
          draft.submitError = error.message;
          rerender();
        }
      },
      "primary",
    );
    section.append(roster, start);
    if (draft.submitError) {
      const error = text("p", `MCP 拒绝：${draft.submitError}`, "error encounter-submit-error");
      error.setAttribute("role", "alert");
      section.append(error);
    }
    return section;
  }

  function buildGridEncounterPlanner(draft, templates, rerender) {
    const planner = text("section", "", "encounter-planner");
    const source = buildEncounterMapSource(draft, templates, rerender);
    const validation = encounterValidation(draft, templates);
    const workspace = text("div", "", "encounter-workspace");
    const board = buildEncounterBoard(draft, templates, validation, rerender);
    const rail = buildEncounterReadinessRail(draft, templates, validation, rerender);
    workspace.append(board, rail);
    const foot = text("footer", "", "encounter-command-foot");
    const readiness = text(
      "span",
      validation.ready
        ? `${draft.selectedIds.length}/${draft.selectedIds.length} READY`
        : `${draft.selectedIds.filter((id) => !(validation.byActor[id] || []).length).length}/${draft.selectedIds.length} READY`,
      validation.ready ? "encounter-readiness ready" : "encounter-readiness",
    );
    const review = button("检查 MCP 载荷", () => {
      draft.reviewOpen = true;
      draft.submitError = "";
      rerender();
    }, "primary");
    review.disabled = !validation.ready;
    review.title = validation.ready ? "查看将提交的精确载荷" : "先处理全部部署错误";
    foot.append(
      button("取消并保留草稿", () => {
        draft.plannerOpen = false;
        draft.reviewOpen = false;
        rerender();
      }),
      text(
        "span",
        "本地提示不裁定移动或落点合法性；最终结果始终由 D&D MCP 校验。",
        "small muted encounter-authority-note",
      ),
      readiness,
      review,
    );
    planner.append(source, workspace, foot);
    if (draft.reviewOpen) planner.append(buildEncounterReview(draft, templates, rerender));
    return planner;
  }

  function buildEncounterMapSource(draft, templates, rerender) {
    const section = text("fieldset", "", "encounter-map-source");
    section.append(text("legend", "地图权威 / 五尺方格"));
    const templateLabel = text("label", "", "encounter-source-option");
    const templateRadio = document.createElement("input");
    templateRadio.type = "radio";
    templateRadio.name = "map_authority";
    templateRadio.value = "template";
    templateRadio.checked = draft.sourceKind === "template";
    templateRadio.disabled = templates.length === 0;
    templateRadio.onchange = () => {
      draft.sourceKind = "template";
      draft.templateId = draft.templateId || templates[0]?.id || "";
      seedEncounterPlacements(draft, templates, { reset: true });
      draft.submitError = "";
      rerender();
    };
    const templateSelect = document.createElement("select");
    templateSelect.setAttribute("aria-label", "模块战斗地图模板");
    templateSelect.disabled = !templateRadio.checked || templates.length === 0;
    if (!templates.length) {
      const option = text("option", "当前场景没有可用模板");
      option.value = "";
      templateSelect.append(option);
    }
    for (const template of templates) {
      const option = text(
        "option",
        `${template.title} · ${template.width}×${template.height} 格`,
      );
      option.value = template.id;
      option.selected = template.id === draft.templateId;
      templateSelect.append(option);
    }
    templateSelect.onchange = () => {
      draft.templateId = templateSelect.value;
      draft.cursor = { x: 0, y: 0 };
      seedEncounterPlacements(draft, templates, { reset: true });
      draft.submitError = "";
      rerender();
    };
    templateLabel.append(templateRadio, text("span", "模块模板"), templateSelect);

    const overrideLabel = text("label", "", "encounter-source-option");
    const overrideRadio = document.createElement("input");
    overrideRadio.type = "radio";
    overrideRadio.name = "map_authority";
    overrideRadio.value = "override";
    overrideRadio.checked = draft.sourceKind === "override";
    overrideRadio.onchange = () => {
      draft.sourceKind = "override";
      draft.cursor = { x: 0, y: 0 };
      seedEncounterPlacements(draft, templates, { reset: true });
      draft.submitError = "";
      rerender();
    };
    const dimensions = text("span", "", "encounter-dimensions");
    for (const [field, label] of [["width", "宽"], ["height", "高"]]) {
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.max = "200";
      input.step = "1";
      input.value = draft.override[field];
      input.disabled = !overrideRadio.checked;
      input.setAttribute("aria-label", `${label}度（格）`);
      input.onchange = () => {
        draft.override[field] = Number(input.value);
        draft.cursor = { x: 0, y: 0 };
        seedEncounterPlacements(draft, templates, { reset: true });
        draft.submitError = "";
        rerender();
      };
      dimensions.append(text("span", label, "small muted"), input);
    }
    dimensions.append(text("span", "格 · 5 ft/格", "small muted"));
    overrideLabel.append(overrideRadio, text("span", "临时空白图"), dimensions);
    const map = mapForEncounter(draft, templates);
    const context = text(
      "p",
      map && Number.isInteger(map.width) && Number.isInteger(map.height)
        ? `${map.width}×${map.height} 格 · ${map.width * 5}×${map.height * 5} ft · 当前 MCP revision ${state.panel?.revision ?? "—"}`
        : `尺寸待修正 · 当前 MCP revision ${state.panel?.revision ?? "—"}`,
      "small muted encounter-map-context",
    );
    section.append(templateLabel, overrideLabel, context);
    return section;
  }

  function buildEncounterReadinessRail(draft, templates, validation, rerender) {
    const rail = text("aside", "", "encounter-readiness-rail");
    rail.append(
      text("span", "ROSTER // READINESS", "encounter-kicker"),
      text("p", "选择角色，再拖动、点按方格或直接输入坐标。", "small muted"),
    );
    for (const actor of draft.actors) {
      const selected = draft.selectedIds.includes(actor.id);
      const issues = validation.byActor[actor.id] || [];
      const position = draft.placements[actor.id];
      const row = text(
        "div",
        "",
        `encounter-roster-row${draft.activeActorId === actor.id ? " active" : ""}${issues.length ? " invalid" : ""}`,
      );
      const head = text("div", "", "encounter-roster-head");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = selected;
      check.setAttribute("aria-label", `${actor.name} 参战`);
      check.onchange = () => {
        toggleEncounterActor(draft, actor.id, check.checked);
        if (check.checked) seedEncounterPlacements(draft, templates);
        rerender();
      };
      const choose = button(actor.name, () => {
        draft.activeActorId = actor.id;
        draft.submitError = "";
        rerender();
      }, "encounter-actor-select");
      choose.setAttribute("aria-label", `选择 ${actor.name} 进行部署`);
      choose.title = "选择后可点按、拖放或使用键盘部署";
      choose.disabled = !selected;
      const stateLabel = text(
        "span",
        !selected ? "STANDBY" : issues.length ? "ERROR" : "PLACED",
        issues.length ? "encounter-actor-state error" : "encounter-actor-state",
      );
      head.append(check, choose, stateLabel);
      row.append(head);
      if (selected) {
        const coords = text("div", "", "encounter-coordinate-inputs");
        for (const axis of ["x", "y"]) {
          const label = text("label", axis.toUpperCase());
          const input = document.createElement("input");
          input.type = "number";
          input.step = "1";
          input.min = "0";
          input.value = position?.[axis] ?? "";
          input.setAttribute("aria-label", `${actor.name} ${axis.toUpperCase()} 坐标`);
          input.onchange = () => {
            const next = {
              x: axis === "x" ? input.value : draft.placements[actor.id]?.x,
              y: axis === "y" ? input.value : draft.placements[actor.id]?.y,
            };
            placeEncounterActor(draft, actor.id, next.x, next.y);
            rerender();
          };
          label.append(input);
          coords.append(label);
        }
        const clear = button("撤下", () => {
          delete draft.placements[actor.id];
          draft.submitError = "";
          rerender();
        });
        clear.disabled = !position;
        coords.append(clear);
        row.append(coords);
        row.append(
          text(
            "p",
            issues.length ? issues.join("；") : `已部署于 ${position.x},${position.y}`,
            issues.length ? "encounter-actor-issues error" : "encounter-actor-issues small muted",
          ),
        );
      }
      rail.append(row);
    }
    for (const message of validation.global) {
      rail.append(text("p", message, "error encounter-global-error"));
    }
    return rail;
  }

  function buildEncounterBoard(draft, templates, validation, rerender) {
    const section = text("section", "", "encounter-deployment-board");
    const head = text("header", "", "encounter-board-head");
    head.append(
      text("span", "DEPLOYMENT BOARD", "encounter-kicker"),
      text(
        "span",
        draft.activeActorId
          ? `当前部署：${draft.actors.find((actor) => actor.id === draft.activeActorId)?.name || draft.activeActorId}`
          : "请在右侧选择参战者",
        "small muted",
      ),
    );
    const wrap = text("div", "", "encounter-board-wrap");
    const canvas = document.createElement("canvas");
    canvas.className = "encounter-board";
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "application");
    canvas.setAttribute("aria-label", "遭遇部署坐标网格");
    canvas.setAttribute("aria-describedby", "encounter-board-status");
    canvas.style.touchAction = "none";
    const status = text(
      "p",
      "方向键移动光标，Enter 部署当前角色；Delete 撤下。",
      "encounter-board-status",
    );
    status.id = "encounter-board-status";
    status.setAttribute("aria-live", "polite");
    wrap.append(canvas);
    section.append(head, wrap, status);
    let dragPreview = null;
    const draw = () => drawEncounterBoard(canvas, draft, validation, dragPreview);
    requestAnimationFrame(draw);
    let draggingActor = "";
    const updateDragPreview = (event) => {
      const pointer = encounterBoardPointer(event, canvas, validation.map);
      if (!pointer || !draggingActor) return null;
      const feedback = encounterPlacementFeedback(
        draft,
        templates,
        draggingActor,
        pointer,
      );
      dragPreview = { actorId: draggingActor, cell: pointer, feedback };
      if (pointer.inBounds) draft.cursor = { x: pointer.x, y: pointer.y };
      const actor = draft.actors.find((item) => item.id === draggingActor);
      status.textContent = feedback.valid
        ? `${actor?.name || draggingActor} → ${pointer.x},${pointer.y} · 松开以部署`
        : `${actor?.name || draggingActor} → ${pointer.x},${pointer.y} · ${feedback.issues.join("；")}（本地非权威提示）`;
      draw();
      return pointer;
    };
    canvas.onpointerdown = (event) => {
      const cell = encounterBoardPointer(event, canvas, validation.map);
      if (!cell?.inBounds) return;
      const placed = draft.selectedIds.find((id) => {
        const position = draft.placements[id];
        return position?.x === cell.x && position?.y === cell.y;
      });
      draggingActor = placed || draft.activeActorId;
      if (draggingActor) draft.activeActorId = draggingActor;
      draft.cursor = { x: cell.x, y: cell.y };
      canvas.setPointerCapture?.(event.pointerId);
      if (draggingActor) updateDragPreview(event);
      else status.textContent = `坐标 ${cell.x},${cell.y}；请先从 readiness rail 选择参战者`;
    };
    canvas.onpointermove = (event) => {
      if (draggingActor) updateDragPreview(event);
    };
    canvas.onpointerup = (event) => {
      const cell = updateDragPreview(event);
      const actorIdValue = draggingActor;
      const feedback = dragPreview?.feedback;
      if (cell && actorIdValue && feedback?.valid) {
        placeEncounterActor(draft, actorIdValue, cell.x, cell.y);
        status.textContent = `已将 ${draft.actors.find((actor) => actor.id === actorIdValue)?.name || actorIdValue} 部署于 ${cell.x},${cell.y}`;
      }
      draggingActor = "";
      dragPreview = null;
      if (cell && actorIdValue && feedback?.valid) rerender();
      else draw();
    };
    canvas.onpointercancel = () => {
      draggingActor = "";
      dragPreview = null;
      status.textContent = "拖动已取消；原部署坐标保持不变。";
      draw();
    };
    canvas.onfocus = () => {
      if (!draft.cursor) draft.cursor = { x: 0, y: 0 };
      draw();
    };
    canvas.onkeydown = (event) => {
      const navigation = new Set([
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
      ]);
      if (navigation.has(event.key) && validation.map) {
        event.preventDefault();
        draft.cursor = moveEncounterCursor(draft.cursor, event.key, validation.map);
        status.textContent = `坐标 ${draft.cursor.x},${draft.cursor.y}`;
        draw();
      } else if ((event.key === "Enter" || event.key === " ") && draft.activeActorId) {
        event.preventDefault();
        const feedback = encounterPlacementFeedback(
          draft,
          templates,
          draft.activeActorId,
          draft.cursor,
        );
        if (feedback.valid) {
          placeEncounterActor(draft, draft.activeActorId, draft.cursor.x, draft.cursor.y);
          rerender();
        } else {
          status.textContent = `${feedback.issues.join("；")}（本地非权威提示，最终由 MCP 校验）`;
          draw();
        }
      } else if ((event.key === "Delete" || event.key === "Backspace") && draft.activeActorId) {
        event.preventDefault();
        delete draft.placements[draft.activeActorId];
        draft.submitError = "";
        rerender();
      }
    };
    return section;
  }

  function encounterBoardMetrics(canvas, map) {
    if (!map || !Number.isInteger(map.width) || !Number.isInteger(map.height)) return null;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const cell = Math.max(3, Math.min(canvas.width / map.width, canvas.height / map.height));
    return {
      dpr,
      cell,
      offsetX: (canvas.width - cell * map.width) / 2,
      offsetY: (canvas.height - cell * map.height) / 2,
    };
  }

  function encounterBoardPointer(event, canvas, map) {
    const metrics = encounterBoardMetrics(canvas, map);
    if (!metrics) return null;
    const rect = canvas.getBoundingClientRect();
    const x = Math.floor(
      ((event.clientX - rect.left) * metrics.dpr - metrics.offsetX) / metrics.cell,
    );
    const y = Math.floor(
      ((event.clientY - rect.top) * metrics.dpr - metrics.offsetY) / metrics.cell,
    );
    return {
      x,
      y,
      inBounds: x >= 0 && y >= 0 && x < map.width && y < map.height,
    };
  }

  function drawEncounterToken(context, metrics, actor, position, options = {}) {
    const centerX = metrics.offsetX + (position.x + 0.5) * metrics.cell;
    const centerY = metrics.offsetY + (position.y + 0.5) * metrics.cell;
    const radius = Math.max(4, metrics.cell * 0.34);
    context.save();
    context.globalAlpha = options.ghost ? 0.72 : 1;
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.fillStyle = options.invalid ? "#a55043" : "#2f9a5c";
    context.fill();
    context.strokeStyle = options.active ? "#f1c66e" : "#e9eee9";
    context.lineWidth = Math.max(1.5, metrics.dpr * 1.3);
    if (options.ghost) context.setLineDash([4 * metrics.dpr, 3 * metrics.dpr]);
    context.stroke();
    if (metrics.cell >= 18) {
      context.fillStyle = "#ffffff";
      context.font = `${Math.max(9, Math.floor(metrics.cell * 0.28))}px system-ui`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(actor.name.slice(0, 2), centerX, centerY);
    }
    context.restore();
  }

  function drawEncounterBoard(canvas, draft, validation, dragPreview = null) {
    const map = validation.map;
    const metrics = encounterBoardMetrics(canvas, map);
    if (!metrics) return;
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#090d0b";
    context.fillRect(0, 0, canvas.width, canvas.height);
    const blocked = new Set((map.blockedCells || []).map((cell) => `${cell.x},${cell.y}`));
    const difficult = new Set((map.difficultCells || []).map((cell) => `${cell.x},${cell.y}`));
    for (let y = 0; y < map.height; y += 1) {
      for (let x = 0; x < map.width; x += 1) {
        const left = metrics.offsetX + x * metrics.cell;
        const top = metrics.offsetY + y * metrics.cell;
        const key = `${x},${y}`;
        context.fillStyle = blocked.has(key)
          ? "#513531"
          : difficult.has(key)
            ? "#5b542d"
            : (x + y) % 2
              ? "#101713"
              : "#0d130f";
        context.fillRect(left, top, metrics.cell, metrics.cell);
        context.strokeStyle = "rgba(118, 154, 127, .34)";
        context.lineWidth = Math.max(1, metrics.dpr * 0.55);
        context.strokeRect(left, top, metrics.cell, metrics.cell);
      }
    }
    for (const actor of draft.actors.filter((item) => draft.selectedIds.includes(item.id))) {
      if (actor.id === dragPreview?.actorId) continue;
      const position = draft.placements[actor.id];
      if (!position || !Number.isInteger(position.x) || !Number.isInteger(position.y)) continue;
      if (position.x < 0 || position.y < 0 || position.x >= map.width || position.y >= map.height) continue;
      drawEncounterToken(context, metrics, actor, position, {
        active: actor.id === draft.activeActorId,
        invalid: (validation.byActor[actor.id] || []).length > 0,
      });
    }
    if (dragPreview?.cell?.inBounds) {
      const cell = dragPreview.cell;
      const valid = dragPreview.feedback.valid;
      context.save();
      context.fillStyle = valid ? "rgba(80, 181, 111, .2)" : "rgba(191, 75, 62, .28)";
      context.fillRect(
        metrics.offsetX + cell.x * metrics.cell,
        metrics.offsetY + cell.y * metrics.cell,
        metrics.cell,
        metrics.cell,
      );
      context.strokeStyle = valid ? "#70d18c" : "#ef7768";
      context.lineWidth = Math.max(2, metrics.dpr * 1.5);
      context.setLineDash([5 * metrics.dpr, 3 * metrics.dpr]);
      context.strokeRect(
        metrics.offsetX + cell.x * metrics.cell + 1,
        metrics.offsetY + cell.y * metrics.cell + 1,
        metrics.cell - 2,
        metrics.cell - 2,
      );
      context.restore();
      const actor = draft.actors.find((item) => item.id === dragPreview.actorId);
      if (actor) {
        drawEncounterToken(context, metrics, actor, cell, {
          active: true,
          ghost: true,
          invalid: !valid,
        });
      }
    }
    if (draft.cursor && draft.cursor.x < map.width && draft.cursor.y < map.height) {
      context.strokeStyle = "#f1c66e";
      context.lineWidth = Math.max(2, metrics.dpr * 1.5);
      context.strokeRect(
        metrics.offsetX + draft.cursor.x * metrics.cell + 1,
        metrics.offsetY + draft.cursor.y * metrics.cell + 1,
        metrics.cell - 2,
        metrics.cell - 2,
      );
    }
  }

  function buildEncounterReview(draft, templates, rerender) {
    const drawer = text("section", "", "encounter-review-drawer");
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-label", "检查遭遇 MCP 载荷");
    const payload = buildEncounterPayload(draft, templates);
    drawer.append(
      text("span", "FINAL CHECK // SERVICE ACTION", "encounter-kicker"),
      text("h3", "提交前精确载荷"),
      text(
        "p",
        `草稿 revision ${draft.revisionAtDraft}；当前投影 revision ${state.panel?.revision ?? "—"}。服务会在提交时读取最新 revision，并由 MCP 最终校验。`,
        "small muted",
      ),
    );
    const preview = text(
      "pre",
      JSON.stringify({ action: "combat.start", payload }, null, 2),
      "encounter-payload-preview",
    );
    drawer.append(preview);
    if (draft.submitError) {
      const error = text(
        "p",
        `D&D MCP 拒绝：${draft.submitError}。草稿未丢失，可返回修改后重试。`,
        "error encounter-submit-error",
      );
      error.setAttribute("role", "alert");
      drawer.append(error);
    }
    const actions = text("div", "", "encounter-review-actions");
    actions.append(
      button("返回部署", () => {
        draft.reviewOpen = false;
        rerender();
      }),
      button(
        "确认并交给 D&D MCP",
        async () => {
          draft.submitError = "";
          try {
            await sendPanelAction("combat.start", payload);
            state.encounterDraft = null;
          } catch (error) {
            draft.submitError = error.message;
            draft.reviewOpen = true;
            rerender();
          }
        },
        "primary",
      ),
    );
    drawer.append(actions);
    drawer.onkeydown = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      draft.reviewOpen = false;
      rerender();
    };
    requestAnimationFrame(() => drawer.querySelector("button")?.focus());
    return drawer;
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
    expand.setAttribute("aria-expanded", String(state.gridExpanded));
    const snapshotToggle = button("玩家分享图", () =>
      loadCombatSnapshot(
        snapshotPanel,
        snapshotImage,
        snapshotToggle,
        snapshotStatus,
        snapshotShare,
        snapshotDownload,
      ),
    );
    snapshotToggle.title = "加载由 D&D MCP 按全队公开视图生成的战况图";
    const headActions = text("div", "", "grid-head-actions");
    headActions.append(snapshotToggle, expand);
    head.append(headActions);
    const snapshotPanel = text("section", "", "combat-snapshot");
    snapshotPanel.hidden = true;
    snapshotPanel.setAttribute("role", "dialog");
    snapshotPanel.setAttribute("aria-modal", "true");
    snapshotPanel.setAttribute("aria-label", "全队公开战况图预览");
    const snapshotImage = document.createElement("img");
    snapshotImage.alt = "D&D MCP 按全队公开视图生成的战况图";
    snapshotImage.hidden = true;
    const snapshotStatus = text(
      "p",
      "尚未加载战况图。",
      "combat-snapshot-status",
    );
    snapshotStatus.setAttribute("role", "status");
    const snapshotShare = button("分享到群聊", shareCombatSnapshot, "primary");
    const snapshotDownload = button("下载 PNG", downloadCombatSnapshot);
    snapshotShare.disabled = true;
    snapshotDownload.disabled = true;
    const snapshotActions = text("div", "", "combat-snapshot-actions");
    snapshotActions.append(
      snapshotShare,
      snapshotDownload,
      button("关闭预览", () => {
        snapshotPanel.hidden = true;
        snapshotToggle.focus();
      }),
    );
    snapshotPanel.append(
      snapshotStatus,
      snapshotImage,
      text(
        "p",
        "此图固定使用全队公开投影，不含 DM 私密字段；战斗状态仍以 MCP 为准。",
        "small muted combat-snapshot-note",
      ),
      snapshotActions,
    );
    snapshotPanel.onkeydown = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      snapshotPanel.hidden = true;
      snapshotToggle.focus();
    };
    const wrap = text("div", "", "grid-canvas-wrap");
    const canvas = document.createElement("canvas");
    const tooltip = text("div", "", "grid-tooltip");
    const status = text("p", "使用方向键移动坐标光标，Enter 选择，Escape 清除。", "grid-status");
    status.id = "combat-grid-status";
    status.setAttribute("aria-live", "polite");
    canvas.id = "combat-grid";
    canvas.className = "combat-grid";
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "application");
    canvas.setAttribute("aria-label", "战斗网格坐标选择器");
    canvas.setAttribute("aria-describedby", "combat-grid-status combat-grid-legend");
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
      snapshotPanel,
      wrap,
      initiative,
      text(
        "div",
        `点击己方角色设为行动者；选择目标或空格建立聊天行动上下文。${MOVEMENT_INTENT_DISCLAIMER}`,
        "grid-legend",
      ),
      status,
    );
    shell.querySelector(".grid-legend").id = "combat-grid-legend";
    shell.classList.toggle("expanded", state.gridExpanded);
    canvas.onpointermove = (event) => gridPointerMove(event, canvas, tooltip);
    canvas.onpointerdown = (event) => {
      const cell = gridCell(event, canvas);
      if (cell) {
        state.gridCursor = cell;
        updateGridStatus(cell, status);
        drawCombatGrid();
      }
    };
    canvas.onpointerleave = () => {
      tooltip.hidden = true;
    };
    canvas.onclick = (event) => gridClick(event, canvas, status);
    canvas.onfocus = () => {
      const bounds = mapBounds(battleMap());
      state.gridCursor = clampGridCursor(
        state.gridCursor || state.gridDestination || actingActorPosition() || { x: 0, y: 0 },
        bounds,
      );
      updateGridStatus(state.gridCursor, status);
      drawCombatGrid();
    };
    canvas.onkeydown = (event) => gridKeyDown(event, canvas, status);
    return shell;
  }

  async function loadCombatSnapshot(panel, image, control, status, share, download) {
    if (combatSnapshotBlob && combatSnapshotUrl) {
      panel.hidden = false;
      image.src = combatSnapshotUrl;
      image.hidden = false;
      status.textContent = "全队公开战况图已载入。";
      share.disabled = false;
      download.disabled = false;
      panel.querySelector("button")?.focus();
      return;
    }
    panel.hidden = false;
    panel.setAttribute("aria-busy", "true");
    image.hidden = true;
    status.textContent = "正在从 D&D MCP 生成全队公开战况图…";
    share.disabled = true;
    download.disabled = true;
    control.disabled = true;
    control.textContent = "正在生成…";
    try {
      const { blob, headers } = await apiBlobResponse(
        `/api/campaigns/${state.campaign.id}/room/combat/render`,
      );
      if (blob.type !== "image/png") throw new Error("战况图格式无效");
      releaseCombatSnapshot();
      combatSnapshotBlob = blob;
      combatSnapshotUrl = URL.createObjectURL(blob);
      image.alt = decodePublicTextHeader(
        headers.get("X-SagaSmith-Combat-Alt"),
        "D&D MCP 按全队公开视图生成的战况图",
      );
      combatSnapshotCaption = decodePublicTextHeader(
        headers.get("X-SagaSmith-Combat-Caption"),
        combatSnapshotCaption,
      );
      image.src = combatSnapshotUrl;
      await image.decode();
      image.hidden = false;
      status.textContent = "全队公开战况图已载入，可分享或下载。";
      share.disabled = false;
      download.disabled = false;
      panel.querySelector("button")?.focus();
      if (!window.matchMedia("(max-width: 600px)").matches) {
        panel.scrollIntoView({
          block: "nearest",
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
            ? "auto"
            : "smooth",
        });
      }
    } catch (error) {
      releaseCombatSnapshot();
      image.removeAttribute("src");
      image.hidden = true;
      status.textContent = `战况图加载失败：${error.message}`;
      toast(`战况图加载失败：${error.message}`);
    } finally {
      panel.setAttribute("aria-busy", "false");
      control.disabled = false;
      control.textContent = "玩家分享图";
    }
  }

  function downloadCombatSnapshot() {
    if (!combatSnapshotBlob || !combatSnapshotUrl) return toast("请先加载玩家分享图");
    const link = document.createElement("a");
    link.href = combatSnapshotUrl;
    link.download = "sagasmith-party-combat.png";
    link.click();
    toast("战况图已下载");
  }

  async function shareCombatSnapshot() {
    if (!combatSnapshotBlob) return toast("请先加载玩家分享图");
    if (typeof File === "undefined") {
      downloadCombatSnapshot();
      return;
    }
    const file = new File([combatSnapshotBlob], "sagasmith-party-combat.png", {
      type: "image/png",
    });
    const shareData = {
      files: [file],
      title: battleMap()?.name || activeCombat().name || "SagaSmith 战况图",
      text: `${combatSnapshotCaption}\n战斗状态与移动判定以 SagaSmith MCP 为准。`,
    };
    if (!navigator.share || (navigator.canShare && !navigator.canShare(shareData))) {
      downloadCombatSnapshot();
      return;
    }
    try {
      await navigator.share(shareData);
    } catch (error) {
      if (error?.name !== "AbortError") {
        toast("当前浏览器无法分享，已改为下载 PNG");
        downloadCombatSnapshot();
      }
    }
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

  function actingActorPosition() {
    const actor = combatants().find(
      (item) => combatantId(item) === state.actingActorId,
    );
    return actor?.position || actor?.coordinates || null;
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

  function drawGridTexture(context, metrics) {
    if (!gridTexture.complete || !gridTexture.naturalWidth || !gridTexture.naturalHeight) return;
    const width = metrics.bounds.width * metrics.cell;
    const height = metrics.bounds.height * metrics.cell;
    const scale = Math.max(width / gridTexture.naturalWidth, height / gridTexture.naturalHeight);
    const drawnWidth = gridTexture.naturalWidth * scale;
    const drawnHeight = gridTexture.naturalHeight * scale;
    context.save();
    context.beginPath();
    context.rect(metrics.offsetX, metrics.offsetY, width, height);
    context.clip();
    context.globalAlpha = 0.14;
    context.drawImage(
      gridTexture,
      metrics.offsetX + (width - drawnWidth) / 2,
      metrics.offsetY + (height - drawnHeight) / 2,
      drawnWidth,
      drawnHeight,
    );
    context.restore();
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
    drawGridTexture(context, metrics);
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
    const intent = movementIntentSegment(
      combatants(),
      state.actingActorId,
      state.gridDestination,
    );
    if (intent) {
      context.save();
      context.strokeStyle = "#f2c76f";
      context.lineWidth = Math.max(2, metrics.dpr * 1.5);
      context.setLineDash([Math.max(5, metrics.cell * 0.22), Math.max(4, metrics.cell * 0.14)]);
      context.beginPath();
      context.moveTo(
        metrics.offsetX + (intent.from.x + 0.5) * metrics.cell,
        metrics.offsetY + (intent.from.y + 0.5) * metrics.cell,
      );
      context.lineTo(
        metrics.offsetX + (intent.to.x + 0.5) * metrics.cell,
        metrics.offsetY + (intent.to.y + 0.5) * metrics.cell,
      );
      context.stroke();
      context.restore();
    }
    if (state.gridCursor) {
      const cursor = clampGridCursor(state.gridCursor, metrics.bounds);
      context.save();
      context.strokeStyle = "#f5efe0";
      context.lineWidth = Math.max(2, metrics.dpr * 1.25);
      context.setLineDash([Math.max(3, metrics.cell * 0.14), Math.max(2, metrics.cell * 0.08)]);
      context.strokeRect(
        metrics.offsetX + cursor.x * metrics.cell + metrics.dpr,
        metrics.offsetY + cursor.y * metrics.cell + metrics.dpr,
        metrics.cell - metrics.dpr * 2,
        metrics.cell - metrics.dpr * 2,
      );
      context.restore();
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
      if (current === id) {
        context.save();
        context.beginPath();
        context.arc(centerX, centerY, radius + Math.max(4, metrics.cell * 0.1), 0, Math.PI * 2);
        context.strokeStyle = "#f2c76f";
        context.lineWidth = Math.max(2, metrics.dpr);
        context.stroke();
        context.fillStyle = "#f2c76f";
        context.beginPath();
        context.moveTo(centerX, centerY - radius - Math.max(8, metrics.cell * 0.18));
        context.lineTo(centerX - Math.max(4, metrics.cell * 0.08), centerY - radius - 3);
        context.lineTo(centerX + Math.max(4, metrics.cell * 0.08), centerY - radius - 3);
        context.closePath();
        context.fill();
        context.restore();
      }
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

  function updateGridStatus(cell, status) {
    if (!cell || !status) return;
    const item = combatantAtCell(combatants(), cell);
    const parts = [`坐标 ${cell.x}, ${cell.y}`, terrainAt(battleMap(), cell)];
    if (item) parts.push(`占用者 ${combatantName(combatantId(item))}`);
    if (state.gridDestination?.x === cell.x && state.gridDestination?.y === cell.y) {
      parts.push(MOVEMENT_INTENT_DISCLAIMER);
    }
    status.textContent = parts.join("；");
  }

  function gridPointerMove(event, canvas, tooltip) {
    const cell = gridCell(event, canvas);
    if (!cell) {
      tooltip.hidden = true;
      return;
    }
    const item = combatantAtCell(combatants(), cell);
    const terrain = terrainAt(battleMap(), cell);
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
    tooltip.style.left = `${Math.max(8, Math.min(event.clientX - rect.left + 12, rect.width - 248))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(event.clientY - rect.top + 12, rect.height - 138))}px`;
    tooltip.hidden = false;
  }

  async function selectGridCell(cell, status) {
    const item = combatantAtCell(combatants(), cell);
    if (item) {
      await focusCombatant(combatantId(item));
      updateGridStatus(cell, status);
      return;
    }
    if (state.actingActorId) {
      state.gridDestination = cell;
      state.selectedTargetId = null;
      renderActionContext();
      drawCombatGrid();
      updateGridStatus(cell, status);
    }
  }

  async function gridClick(event, canvas, status) {
    const cell = gridCell(event, canvas);
    if (!cell) return;
    state.gridCursor = cell;
    await selectGridCell(cell, status);
  }

  async function gridKeyDown(event, canvas, status) {
    const navigationKeys = new Set([
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "ArrowDown",
      "Home",
      "End",
    ]);
    if (navigationKeys.has(event.key)) {
      event.preventDefault();
      state.gridCursor = moveGridCursor(
        state.gridCursor || { x: 0, y: 0 },
        event.key,
        mapBounds(battleMap()),
      );
      updateGridStatus(state.gridCursor, status);
      drawCombatGrid();
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (state.gridCursor) await selectGridCell(state.gridCursor, status);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      state.gridDestination = null;
      state.selectedTargetId = null;
      renderActionContext();
      drawCombatGrid();
      updateGridStatus(state.gridCursor, status);
    }
  }

  function initialize() {
    window.addEventListener("resize", () => requestAnimationFrame(drawCombatGrid));
    gridTexture.addEventListener("load", () => requestAnimationFrame(drawCombatGrid), {
      once: true,
    });
  }

  return {
    drawCombatGrid,
    initialize,
    renderCombatPanel,
    setGridExpanded,
  };
}
