import { api } from "/assets/api/client.js";
import { $, $$, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import {
  actorHp,
  actorId,
  actorName,
  asList,
  canControl,
  canInspect,
  cardRecord,
  characterActor,
  characters,
  combatantName,
} from "/assets/room/model.js";
import { appendDetails, entries, num, signed } from "/assets/room/view.js";
import { state } from "/assets/state/store.js";

const abilityLabels = {
  str: "力量",
  dex: "敏捷",
  con: "体质",
  int: "智力",
  wis: "感知",
  cha: "魅力",
  strength: "力量",
  dexterity: "敏捷",
  constitution: "体质",
  intelligence: "智力",
  wisdom: "感知",
  charisma: "魅力",
};

const skillLabels = {
  acrobatics: "杂技",
  animal_handling: "驯兽",
  arcana: "奥秘",
  athletics: "运动",
  deception: "欺瞒",
  history: "历史",
  insight: "洞悉",
  intimidation: "威吓",
  investigation: "调查",
  medicine: "医药",
  nature: "自然",
  perception: "察觉",
  performance: "表演",
  persuasion: "游说",
  religion: "宗教",
  sleight_of_hand: "巧手",
  stealth: "隐匿",
  survival: "求生",
};

export function createCharacterController({ sendPanelAction, drawCombatGrid }) {
  function renderActionContext() {
    const root = $("#action-context");
    if (!root) return;
    root.replaceChildren();
    const chips = [];
    if (state.actingActorId) {
      chips.push([
        `行动者：${actorName(
          characterActor(state.actingActorId) || { id: state.actingActorId },
        )}`,
        () => {
          state.actingActorId = null;
          renderActionContext();
          drawCombatGrid();
        },
      ]);
    }
    if (state.selectedTargetId) {
      chips.push([
        `目标：${combatantName(state.selectedTargetId)}`,
        () => {
          state.selectedTargetId = null;
          renderActionContext();
          drawCombatGrid();
        },
      ]);
    }
    if (state.gridDestination) {
      chips.push([
        `移动意图：${state.gridDestination.x}, ${state.gridDestination.y}（待 MCP 校验）`,
        () => {
          state.gridDestination = null;
          renderActionContext();
          drawCombatGrid();
        },
      ]);
    }
    for (const [label, remove] of chips) {
      const chip = text("span", "", "context-chip");
      const close = button("×", remove);
      close.title = "移除动作上下文";
      chip.append(text("span", label), close);
      root.append(chip);
    }
    root.hidden = !chips.length;
  }

  async function loadCharacterCard(id, { quiet = false, force = false } = {}) {
    if (!id || (!force && state.characterCards.has(id))) return cardRecord(id);
    if (!canInspect(id) || state.characterDenied.has(id)) return null;
    try {
      const value = await api(
        `/api/campaigns/${state.campaign.id}/room/characters/${encodeURIComponent(id)}`,
      );
      state.characterCards.set(id, value);
      return value;
    } catch (error) {
      state.characterDenied.add(id);
      if (!quiet) toast(`角色卡不可用：${error.message}`);
      return null;
    }
  }

  async function refreshCharacterSidebar() {
    const visible = characters();
    const inspectable = visible.filter((actor) => canInspect(actorId(actor)));
    if (
      state.inspectedActorId &&
      !visible.some((actor) => actorId(actor) === state.inspectedActorId)
    ) {
      state.inspectedActorId = null;
    }
    if (
      state.actingActorId &&
      !visible.some((actor) => actorId(actor) === state.actingActorId)
    ) {
      state.actingActorId = null;
    }
    if (!state.actingActorId) {
      state.actingActorId =
        actorId(visible.find((actor) => canControl(actorId(actor))) || null) || null;
    }
    if (!state.inspectedActorId) {
      state.inspectedActorId =
        state.actingActorId || actorId(inspectable[0] || null) || null;
    }
    if (state.inspectedActorId) {
      const summary = visible.find((actor) => actorId(actor) === state.inspectedActorId);
      const cached = cardRecord(state.inspectedActorId)?.actor;
      const revisionChanged =
        summary?.revision !== undefined && String(summary.revision) !== String(cached?.revision);
      await loadCharacterCard(state.inspectedActorId, {
        quiet: true,
        force: revisionChanged,
      });
    }
    renderCharacterSidebar();
  }

  function renderCharacterSidebar() {
    const select = $("#character-select");
    const visible = characters();
    const inspectable = visible.filter((actor) => canInspect(actorId(actor)));
    select.replaceChildren(
      ...inspectable.map((actor) => {
        const option = text("option", actorName(actor));
        option.value = actorId(actor);
        return option;
      }),
    );
    select.disabled = !inspectable.length;
    if (state.inspectedActorId) select.value = state.inspectedActorId;
    const record = cardRecord(state.inspectedActorId);
    const actor = record?.actor;
    $("#character-revision").textContent =
      actor?.revision !== undefined ? `rev ${actor.revision}` : "";
    $$('[data-character-page]').forEach((item) =>
      item.classList.toggle("active", item.dataset.characterPage === state.characterPage),
    );
    $$(".character-page").forEach((item) => {
      item.hidden = item.id !== `character-page-${state.characterPage}`;
    });
    renderCharacterPage(actor);
    renderSpellsPage(actor);
    renderInventoryPage(actor);
    renderPartyPage();
    appendCharacterControls(actor);
    const toggle = $("#character-sidebar-toggle");
    toggle.setAttribute("aria-expanded", String(!state.characterCollapsed));
    toggle.title = state.characterCollapsed ? "展开角色侧栏" : "收起角色侧栏";
    toggle.querySelector("span").textContent = state.characterCollapsed ? "▶" : "◀";
    toggle.querySelector("small").textContent = state.characterCollapsed ? "展开" : "收起";
  }

  function appendCharacterControls(actor) {
    const id = actorId(actor);
    if (!id || !canControl(id)) return;
    const addControls = (root, actions) => {
      const controls = text("div", "", "panel-actions character-controls");
      for (const [label, intent, klass] of actions) {
        controls.append(
          button(
            label,
            () => {
              state.actingActorId = id;
              renderActionContext();
              const detail = prompt(intent);
              if (detail) sendPanelAction("character.intent", { actor_id: id, intent: detail });
            },
            klass || "",
          ),
        );
      }
      root.append(controls);
    };
    addControls($("#character-page-character"), [
      ["进行检定", "描述检定，例如：察觉、隐匿或奥秘", "primary"],
      ["使用能力", "描述要使用的能力或特性"],
      ["休息", "描述短休、长休或需要恢复的资源"],
    ]);
    addControls($("#character-page-spells"), [
      ["施放法术", "输入法术、目标、法术位与施法方式", "primary"],
    ]);
    addControls($("#character-page-inventory"), [
      ["使用 / 装备", "输入要使用、装备、卸下或转移的物品", "primary"],
    ]);
  }

  function renderCharacterPage(actor) {
    const root = $("#character-page-character");
    root.replaceChildren();
    if (!actor) {
      root.append(
        text(
          "p",
          canInspect(state.inspectedActorId) ? "正在读取角色卡…" : "你没有可查看的角色卡",
          "empty-state",
        ),
      );
      return;
    }
    const sheet = actor.sheet || {};
    const derived = actor.derived || {};
    const progression = sheet.progression || {};
    const combat = sheet.combat || {};
    const hp = derived.hit_points || combat.hp || {};
    const classes = asList(progression, "classes", "items");
    const level =
      progression.level || classes.reduce((sum, item) => sum + Number(item.level || 0), 0);
    const hero = text("div", "", "character-hero");
    const line = text("div", "", "character-hero-line");
    line.append(text("h3", actorName(actor)), text("span", `Lv ${num(level)}`, "level-badge"));
    hero.append(
      line,
      text(
        "p",
        classes
          .map((item) => `${item.name || item.class_id || item.id || "职业"} ${item.level || ""}`)
          .join(" / ") ||
          [sheet.identity?.species, sheet.identity?.background].filter(Boolean).join(" · ") ||
          actor.character_type ||
          "角色",
        "small muted",
      ),
    );
    root.append(hero);
    const vitals = text("div", "", "vital-grid");
    const vitalData = [
      ["HP", `${num(hp.value ?? hp.current)} / ${num(hp.max ?? hp.maximum)}`],
      ["AC", num(derived.armor_class ?? combat.armor_class)],
      ["先攻", signed(derived.initiative ?? combat.initiative)],
    ];
    for (const [label, value] of vitalData) {
      const box = text("div", "", "vital");
      box.append(text("strong", String(value)), text("small", label));
      vitals.append(box);
    }
    root.append(vitals);
    if (Number(hp.max ?? hp.maximum) > 0) {
      const meter = text("div", "", "hp-meter");
      const fill = text("span", "");
      fill.style.width = `${Math.max(
        0,
        Math.min(
          100,
          (Number((hp.value ?? hp.current) ?? 0) / Number(hp.max ?? hp.maximum)) * 100,
        ),
      )}%`;
      meter.append(fill);
      root.append(meter);
    }
    appendDetails(root, [
      ["临时生命", hp.temp ?? hp.temporary],
      [
        "速度",
        typeof derived.speed === "object"
          ? `${derived.speed.walk || Object.values(derived.speed)[0] || "—"} ft`
          : (derived.speed ?? combat.speed),
      ],
      ["熟练加值", signed(derived.proficiency_bonus ?? progression.proficiency_bonus)],
      ["被动察觉", derived.passive_perception],
      ["护盾/激励", combat.inspiration ?? sheet.inspiration],
      ["力竭", combat.exhaustion ?? sheet.exhaustion],
    ]);
    const scores = derived.ability_scores || sheet.abilities || {};
    const modifiers = derived.ability_modifiers || {};
    if (entries(scores).length) {
      root.append(text("h4", "属性"));
      const grid = text("div", "", "ability-grid");
      for (const [key, value] of entries(scores)) {
        const score = typeof value === "object" ? (value.score ?? value.value) : value;
        const modifier = modifiers[key] ?? (typeof value === "object" ? value.modifier : undefined);
        const box = text("div", "", "ability");
        box.append(
          text("strong", abilityLabels[key] || key.toUpperCase()),
          text("span", `${num(score)} · ${signed(modifier)}`),
        );
        grid.append(box);
      }
      root.append(grid);
    }
    const saves = derived.saving_throws || {};
    if (entries(saves).length) {
      root.append(text("h4", "豁免"));
      appendDetails(
        root,
        entries(saves).map(([key, value]) => [
          abilityLabels[key] || key,
          signed(typeof value === "object" ? (value.modifier ?? value.value) : value),
        ]),
      );
    }
    const skills = derived.skills || sheet.skills || {};
    if (entries(skills).length) {
      root.append(text("h4", "技能"));
      appendDetails(
        root,
        entries(skills).map(([key, value]) => [
          skillLabels[key] || key.replaceAll("_", " "),
          signed(
            typeof value === "object"
              ? (value.modifier ?? value.bonus ?? value.value)
              : value,
          ),
        ]),
      );
    }
    const conditions = asList(sheet, "conditions", "items");
    if (conditions.length) {
      root.append(text("h4", "状态"));
      root.append(
        text(
          "p",
          conditions.map((item) => (typeof item === "string" ? item : item.name || item.id)).join("、"),
          "small",
        ),
      );
    }
    const resources = sheet.resources || {};
    if (entries(resources).length) {
      root.append(text("h4", "资源"));
      for (const [key, value] of entries(resources)) {
        const current = typeof value === "object" ? (value.value ?? value.current) : value;
        const maximum = typeof value === "object" ? (value.max ?? value.maximum) : undefined;
        const row = text("div", "", "resource-row");
        const head = text("div", "", "resource-head");
        head.append(
          text("span", value?.name || key),
          text("span", maximum !== undefined ? `${current}/${maximum}` : String(current)),
        );
        row.append(head);
        if (Number(maximum) > 0) {
          const meter = text("div", "", "resource-meter");
          const fill = text("span", "");
          fill.style.width = `${Math.max(
            0,
            Math.min(100, (Number(current || 0) / Number(maximum)) * 100),
          )}%`;
          meter.append(fill);
          row.append(meter);
        }
        root.append(row);
      }
    }
    const content = sheet.content || {};
    const features = [
      ...asList(content, "features", "items"),
      ...asList(content, "feats", "items"),
    ];
    if (features.length) {
      root.append(text("h4", "特性与专长"));
      for (const feature of features.slice(0, 20)) {
        const details = document.createElement("details");
        details.className = "feature-card";
        details.append(
          text("summary", feature.name || feature.id || "未命名特性"),
          text(
            "p",
            feature.description || feature.summary || feature.text || "没有额外说明",
            "small muted",
          ),
        );
        root.append(details);
      }
    }
  }

  function renderSpellsPage(actor) {
    const root = $("#character-page-spells");
    root.replaceChildren();
    if (!actor) {
      root.append(text("p", "选择一个可查看角色", "empty-state"));
      return;
    }
    const sheet = actor.sheet || {};
    const derived = actor.derived || {};
    const casting = sheet.spellcasting || {};
    const content = sheet.content || {};
    const spells = asList(content, "spells", "items");
    const slots = casting.spell_slots || casting.slots || {};
    root.append(text("h3", "法术"));
    appendDetails(root, [
      ["施法属性", abilityLabels[casting.ability] || casting.ability],
      ["法术攻击", signed(derived.spell_attack_bonus ?? casting.spell_attack_bonus)],
      ["法术豁免 DC", derived.spell_save_dc ?? casting.save_dc],
      ["专注", casting.concentration?.name || casting.concentration],
    ]);
    if (entries(slots).length) {
      root.append(text("h4", "法术位"));
      for (const [level, value] of entries(slots)) {
        const current =
          typeof value === "object" ? (value.value ?? value.current ?? value.remaining) : value;
        const maximum =
          typeof value === "object" ? (value.max ?? value.maximum ?? value.total) : undefined;
        const row = text("div", "", "resource-row");
        const head = text("div", "", "resource-head");
        head.append(
          text("span", `${level}环`),
          text("span", maximum !== undefined ? `${current}/${maximum}` : String(current)),
        );
        row.append(head);
        root.append(row);
      }
    }
    if (!spells.length) {
      root.append(text("p", "没有可用法术记录", "empty-state"));
      return;
    }
    const grouped = Map.groupBy
      ? Map.groupBy(spells, (item) => Number(item.level ?? item.spell_level ?? 0))
      : spells.reduce((map, item) => {
          const key = Number(item.level ?? item.spell_level ?? 0);
          if (!map.has(key)) map.set(key, []);
          map.get(key).push(item);
          return map;
        }, new Map());
    for (const [level, items] of [...grouped].sort((a, b) => a[0] - b[0])) {
      root.append(text("h4", level === 0 ? "戏法" : `${level} 环法术`, "spell-level"));
      for (const spell of items) {
        const details = document.createElement("details");
        details.className = "spell-card";
        details.append(
          text("summary", spell.name || spell.id || "未命名法术"),
          text(
            "div",
            [spell.school, spell.casting_time, spell.range, spell.duration]
              .filter(Boolean)
              .join(" · "),
            "spell-meta",
          ),
          text("p", spell.description || spell.summary || "没有额外说明", "small muted"),
        );
        root.append(details);
      }
    }
  }

  function renderInventoryPage(actor) {
    const root = $("#character-page-inventory");
    root.replaceChildren();
    if (!actor) {
      root.append(text("p", "选择一个可查看角色", "empty-state"));
      return;
    }
    const inventory = actor.sheet?.inventory || {};
    const derived = actor.derived?.inventory || {};
    const items = asList(inventory, "items", "entries");
    root.append(text("h3", "装备与背包"));
    const slots = inventory.equipment_slots || {};
    if (entries(slots).length) {
      root.append(text("h4", "装备栏"));
      const grid = text("div", "", "equipment-slots");
      for (const [slot, value] of entries(slots)) {
        const box = text("div", "", "equipment-slot");
        const itemId = typeof value === "object" ? value.item_id || value.id : value;
        const item = items.find((candidate) => (candidate.id || candidate.item_id) === itemId);
        box.append(
          text("strong", slot.replaceAll("_", " ")),
          text("span", item?.name || value?.name || itemId || "空"),
        );
        grid.append(box);
      }
      root.append(grid);
    }
    const wallet = inventory.wallet || inventory.currency || {};
    if (entries(wallet).length) {
      root.append(text("h4", "钱币"));
      appendDetails(
        root,
        entries(wallet).map(([key, value]) => [key.toUpperCase(), value]),
      );
    }
    const encumbrance = derived.encumbrance || {};
    appendDetails(root, [
      ["负重", encumbrance.current ?? encumbrance.weight],
      ["负重上限", encumbrance.maximum ?? encumbrance.capacity],
    ]);
    root.append(text("h4", "物品"));
    if (!items.length) {
      root.append(text("p", "背包为空", "empty-state"));
      return;
    }
    const byParent = new Map();
    for (const item of items) {
      const key = item.container_id || item.parent_id || "root";
      if (!byParent.has(key)) byParent.set(key, []);
      byParent.get(key).push(item);
    }
    const renderItems = (parent, depth = 0) => {
      for (const item of byParent.get(parent) || []) {
        const details = document.createElement("details");
        details.className = "inventory-item";
        details.style.marginLeft = `${Math.min(depth, 3) * 0.45}rem`;
        details.append(
          text(
            "summary",
            `${item.name || item.id || "物品"}${Number(item.quantity || 1) > 1 ? ` ×${item.quantity}` : ""}`,
          ),
          text(
            "div",
            [
              item.equipped ? "已装备" : "",
              item.attuned ? "已同调" : "",
              item.weight !== undefined ? `${item.weight} lb` : "",
            ]
              .filter(Boolean)
              .join(" · "),
            "item-meta",
          ),
        );
        if (item.description) details.append(text("p", item.description, "small muted"));
        root.append(details);
        renderItems(item.id || item.item_id, depth + 1);
      }
    };
    renderItems("root");
  }

  function renderPartyPage() {
    const root = $("#character-page-party");
    const visible = characters();
    root.replaceChildren(text("h3", "队友摘要"));
    if (!visible.length) {
      root.append(text("p", "当前没有可见队友", "empty-state"));
      return;
    }
    for (const actor of visible) {
      const id = actorId(actor);
      const record = cardRecord(id);
      const privateActor = record?.actor;
      const hp = privateActor
        ? privateActor.derived?.hit_points || privateActor.sheet?.combat?.hp || {}
        : actorHp(actor);
      const card = text("article", "", "party-card");
      if (id === state.actingActorId) card.classList.add("active");
      const header = document.createElement("header");
      header.append(
        text("strong", actorName(actor)),
        text("span", id === state.actingActorId ? "行动中" : ""),
      );
      card.append(header);
      const stats = text("div", "", "party-stats");
      stats.append(
        text(
          "span",
          privateActor
            ? `HP ${num(hp.value ?? hp.current)}/${num(hp.max ?? hp.maximum)}`
            : `HP ${num(hp.value)}/${num(hp.maximum)}`,
        ),
      );
      if (privateActor) stats.append(text("span", `AC ${num(privateActor.derived?.armor_class)}`));
      card.append(stats);
      card.onclick = async () => {
        if (canInspect(id)) {
          state.inspectedActorId = id;
          await loadCharacterCard(id, { quiet: true });
          renderCharacterSidebar();
        }
        if (canControl(id)) state.actingActorId = id;
        state.selectedTargetId = null;
        renderActionContext();
        drawCombatGrid();
      };
      root.append(card);
    }
  }

  function setCharacterPage(page) {
    state.characterPage = page;
    localStorage.setItem(`sagasmith:character-page:${state.campaign.id}`, page);
    renderCharacterSidebar();
  }

  function initialize() {
    $("#character-select").onchange = async (event) => {
      state.inspectedActorId = event.target.value;
      await loadCharacterCard(state.inspectedActorId);
      renderCharacterSidebar();
      drawCombatGrid();
    };
    $$('[data-character-page]').forEach((item) => {
      item.onclick = () => setCharacterPage(item.dataset.characterPage);
    });
    $("#character-sidebar-toggle").onclick = () => {
      state.characterCollapsed = !state.characterCollapsed;
      localStorage.setItem(
        `sagasmith:character-collapsed:${state.campaign.id}`,
        String(state.characterCollapsed),
      );
      $("#campaign-room").classList.toggle("character-collapsed", state.characterCollapsed);
      renderCharacterSidebar();
      requestAnimationFrame(drawCombatGrid);
    };
  }

  return {
    initialize,
    loadCharacterCard,
    refreshCharacterSidebar,
    renderActionContext,
    renderCharacterSidebar,
  };
}
