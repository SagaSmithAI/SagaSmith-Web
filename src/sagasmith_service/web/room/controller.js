import { api } from "/assets/api/client.js";
import { $, $$, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { createCharacterController } from "/assets/room/characters.js";
import { createCombatGridController } from "/assets/room/combat-grid.js";
import { actionContextPayload, asList } from "/assets/room/model.js";
import { createRoomTimelineController } from "/assets/room/timeline.js";
import { state } from "/assets/state/store.js";

export function createRoomController({
  loadCampaigns,
  loadUsage,
  loadCampaignIdentities,
  loadIdentityInviteOptions,
  openModuleStudio,
}) {
  let panelRefreshPromise = null;
  let panelRefreshQueued = false;

  function recordPanelRefresh(result) {
    state.panelRefreshMetrics[result] += 1;
    globalThis.dispatchEvent(
      new CustomEvent("sagasmith:panel-refresh", { detail: { result } }),
    );
  }
  const timelineController = createRoomTimelineController({
    refreshPanel,
    loadUsage,
    loadCampaignIdentities,
    leaveRoom,
  });
  let combatGridController;
  const characterController = createCharacterController({
    sendPanelAction,
    drawCombatGrid: (...args) => combatGridController.drawCombatGrid(...args),
  });
  combatGridController = createCombatGridController({
    sendPanelAction,
    loadCharacterCard: characterController.loadCharacterCard,
    renderCharacterSidebar: characterController.renderCharacterSidebar,
    renderActionContext: characterController.renderActionContext,
    refreshSuggestionValidity: timelineController.refreshSuggestionValidity,
  });

  function applyRoomMode(mode) {
    const dm = ["owner", "dm"].includes(state.membership?.role);
    state.roomMode = mode === "director" && !dm ? "player" : mode;
    const room = $("#campaign-room");
    room.dataset.roomMode = state.roomMode;
    for (const control of $$('[data-room-mode]')) {
      control.hidden = control.dataset.roomMode === "director" && !dm;
      control.classList.toggle("active", control.dataset.roomMode === state.roomMode);
      control.setAttribute("aria-pressed", String(control.dataset.roomMode === state.roomMode));
    }
    for (const control of $$('[data-dm-help]')) control.hidden = !dm;
    if (state.campaign) {
      localStorage.setItem(`sagasmith:room-mode:${state.campaign.id}`, state.roomMode);
    }
  }

  async function openCampaign(campaign) {
    if (state.roomEvents) state.roomEvents.close();
    state.campaign = campaign;
    state.roomMessages = new Map();
    state.panel = null;
    state.roomEventCursor = 0;
    state.animatedResolutions = new Set();
    state.characterCards = new Map();
    state.characterDenied = new Set();
    state.inspectedActorId = null;
    state.actingActorId = null;
    state.selectedTargetId = null;
    state.gridDestination = null;
    state.gridCursor = null;
    state.gridZoom = 1;
    state.gridViewportCenter = null;
    state.gridExpanded = false;
    if (state.encounterDraft?.campaignId !== campaign.id) state.encounterDraft = null;
    state.characterPage =
      localStorage.getItem(`sagasmith:character-page:${campaign.id}`) || "character";
    state.characterCollapsed =
      localStorage.getItem(`sagasmith:character-collapsed:${campaign.id}`) === "true";
    state.roomMode = localStorage.getItem(`sagasmith:room-mode:${campaign.id}`) || "table";
    $("#campaign-room").classList.toggle("character-collapsed", state.characterCollapsed);
    $("#campaign-room").classList.remove("grid-expanded");
    $("#campaign-list").hidden = true;
    $("#invite-accept-form").hidden = true;
    $("#new-campaign").hidden = true;
    $("#campaign-room").hidden = false;
    $("#room-title").textContent = campaign.name;
    const systemNames = {
      dnd5e: "D&D 5E",
      coc7e: "CALL OF CTHULHU 7E",
      narrative: "NARRATIVE",
    };
    $("#room-system").textContent =
      `${systemNames[campaign.system_id] || campaign.system_id.toUpperCase()} · LIVE ROOM`;
    $("#room-sync").textContent = "同步中";
    state.members = await api(`/api/campaigns/${campaign.id}/members`);
    state.membership = state.members.find((member) => member.user_id === state.user.id);
    applyRoomMode(state.roomMode);
    renderMembers();
    await timelineController.loadRoomSnapshot();
    await Promise.all([refreshPanel(), loadCampaignIdentities(), loadDmTools()]);
    timelineController.connectRoomEvents();
  }

  function renderMembers() {
    const root = $("#members");
    root.replaceChildren();
    const canRemove = ["owner", "dm"].includes(state.membership?.role);
    const owner = state.membership?.role === "owner";
    for (const member of state.members) {
      const row = text("div", "", "review-row");
      row.append(text("span", `${member.display_name} · ${member.role}`));
      if (owner && member.role !== "owner") {
        row.append(
          button(
            member.role === "dm" ? "设为玩家" : "设为 DM",
            () => changeMemberRole(member.user_id, member.role === "dm" ? "player" : "dm"),
          ),
        );
      }
      if (canRemove && member.role !== "owner" && member.user_id !== state.user.id) {
        row.append(button("移除", () => removeMember(member.user_id)));
      }
      root.append(row);
    }
    const select = $("#actor-user");
    select.replaceChildren(
      ...state.members.map((member) => {
        const option = text("option", `${member.display_name} · ${member.role}`);
        option.value = member.user_id;
        return option;
      }),
    );
  }

  async function removeMember(userId) {
    if (!confirm("撤销该成员及其全部 Actor 权限？")) return;
    try {
      await api(`/api/campaigns/${state.campaign.id}/members/${userId}`, {
        method: "DELETE",
      });
      toast("成员权限已由 D&D MCP 撤销");
      await openCampaign(state.campaign);
    } catch (error) {
      toast(error.message);
    }
  }

  async function changeMemberRole(userId, role) {
    try {
      await api(`/api/campaigns/${state.campaign.id}/members/${userId}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
      });
      toast("战役角色已由 D&D MCP 更新");
      await openCampaign(state.campaign);
    } catch (error) {
      toast(error.message);
    }
  }

  async function loadDmTools() {
    const dm = ["owner", "dm"].includes(state.membership?.role);
    $("#dm-tools").hidden = !dm;
    if (!dm) return;
    const requests = await api(`/api/campaigns/${state.campaign.id}/join-requests`);
    const root = $("#join-requests");
    root.replaceChildren();
    for (const item of requests.filter((request) => request.status === "pending")) {
      const row = text("div", "", "review-row");
      row.append(
        text("span", `${item.applicant_user_id} ${item.message || ""}`),
        button("批准", () => decideJoin(item.id, "approved"), "primary"),
        button("拒绝", () => decideJoin(item.id, "rejected")),
      );
      root.append(row);
    }
    await loadIdentityInviteOptions();
  }

  async function decideJoin(id, decision) {
    await api(`/api/campaigns/${state.campaign.id}/join-requests/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    toast(decision === "approved" ? "申请已批准" : "申请已拒绝");
    await openCampaign(state.campaign);
  }

  async function loadCampaignPacks() {
    state.packs = await api("/api/packs");
    const imported = await api(`/api/packs/campaigns/${state.campaign.id}`);
    const byId = new Map(imported.map((item) => [item.private_pack_id, item]));
    const owned = new Set(state.packs.map((item) => item.id));
    const root = $("#campaign-packs");
    root.replaceChildren();
    for (const pack of state.packs) {
      const row = text("div", "", "review-row");
      const projection = byId.get(pack.id);
      row.append(text("span", `${pack.title} · ${pack.version}`));
      if (projection?.status === "activated" || pack.kind === "preset") {
        row.append(
          text(
            "span",
            projection?.status === "activated" ? "已激活" : "已导入角色库",
            "phase",
          ),
        );
      } else if (projection) {
        row.append(button("激活", () => activatePack(pack.id), "primary"));
      } else {
        row.append(button("导入", () => importPack(pack.id)));
      }
      root.append(row);
    }
    for (const projection of imported.filter((item) => !owned.has(item.private_pack_id))) {
      const row = text("div", "", "review-row");
      row.append(
        text("span", `战役私有 Pack · ${projection.private_pack_id.slice(0, 8)}`),
      );
      if (projection.status === "activated") {
        row.append(text("span", "已激活", "phase"));
      } else {
        row.append(
          button("激活", () => activatePack(projection.private_pack_id), "primary"),
        );
      }
      root.append(row);
    }
  }

  async function importPack(packId) {
    try {
      await api(`/api/packs/${packId}/campaigns/${state.campaign.id}/import`, {
        method: "POST",
      });
      toast("Pack 已由 D&D MCP 导入");
      loadCampaignPacks();
    } catch (error) {
      toast(error.message);
    }
  }

  async function activatePack(packId) {
    try {
      await api(`/api/packs/${packId}/campaigns/${state.campaign.id}/activate`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      toast("Pack 已由 D&D MCP 激活");
      loadCampaignPacks();
    } catch (error) {
      toast(error.message);
    }
  }

  function leaveRoom() {
    if (state.roomEvents) state.roomEvents.close();
    combatGridController.setGridExpanded(false);
    state.roomEvents = null;
    state.campaign = null;
    state.encounterDraft = null;
    $("#campaign-room").hidden = true;
    $("#campaign-list").hidden = false;
    $("#invite-accept-form").hidden = false;
    $("#new-campaign").hidden = false;
    loadCampaigns();
  }

  async function sendPanelAction(action, payload = {}) {
    try {
      const result = await api(`/api/campaigns/${state.campaign.id}/room/panel/actions`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          action,
          payload,
          base_revision: state.panel?.revision ?? state.campaign?.mcp_revision ?? null,
        }),
      });
      if (result.message) timelineController.updateMessage(result.message);
      if (result.agent_message) timelineController.updateMessage(result.agent_message);
      await refreshPanel();
      loadUsage();
      return result;
    } catch (error) {
      toast(`操作失败：${error.message}`);
      throw error;
    }
  }

  async function fetchPanelOnce() {
    const campaignId = state.campaign?.id;
    if (!campaignId) return;
    const knownRevision = state.panel?.revision;
    const query = Number.isInteger(Number(knownRevision))
      ? `?known_revision=${encodeURIComponent(knownRevision)}`
      : "";
    const panel = await api(`/api/campaigns/${campaignId}/room/panel${query}`);
    if (state.campaign?.id !== campaignId) return;
    if (panel.not_modified) {
      recordPanelRefresh("not_modified");
      return;
    }
    recordPanelRefresh("modified");
    state.panel = panel;
    const phase = state.panel.phase || "—";
    $("#room-phase").textContent = phase.toUpperCase();
    $("#campaign-room").dataset.phase = phase;
    await characterController.refreshCharacterSidebar();
    renderPlayPanel();
    combatGridController.renderCombatPanel();
    renderModulePanel();
    characterController.renderActionContext();
    timelineController.refreshSuggestionValidity();
  }

  function refreshPanel() {
    if (!state.campaign) return Promise.resolve();
    recordPanelRefresh("requested");
    if (panelRefreshPromise) {
      panelRefreshQueued = true;
      recordPanelRefresh("coalesced");
      return panelRefreshPromise;
    }
    panelRefreshPromise = (async () => {
      try {
        do {
          panelRefreshQueued = false;
          await fetchPanelOnce();
        } while (panelRefreshQueued && state.campaign);
      } catch (error) {
        recordPanelRefresh("error");
        $("#room-sync").textContent = "状态读取失败";
        toast(error.message);
      } finally {
        panelRefreshPromise = null;
      }
    })();
    return panelRefreshPromise;
  }

  function renderPlayPanel() {
    const root = $("#play-panel");
    const scene = state.panel?.current_module?.scene || state.panel?.current_module || {};
    const party = state.panel?.party || {};
    const dm = ["owner", "dm"].includes(state.membership?.role);
    root.replaceChildren(
      text("p", scene.title || scene.name || "当前没有激活场景", "panel-title"),
    );
    if (scene.summary || scene.description) {
      root.append(text("p", scene.summary || scene.description, "muted"));
    }
    const clock = state.panel?.campaign?.state?.game_time || party.game_time;
    if (clock) {
      root.append(
        text(
          "p",
          `时间：${
            clock.label ||
            `${clock.day || 0}日 ${clock.hour || 0}:${String(clock.minute || 0).padStart(2, "0")}`
          }`,
          "meta",
        ),
      );
    }
    const controls = text("div", "", "panel-actions");
    controls.append(
      button("描述行动", () => {
        const intent = prompt("你想在当前场景做什么？");
        if (intent) sendPanelAction("play.intent", { ...actionContextPayload(), intent });
      }),
      button("掷骰/检定", () => {
        const intent = prompt("描述要进行的骰点或检定");
        if (intent) sendPanelAction("play.intent", { ...actionContextPayload(), intent });
      }),
    );
    if (dm) {
      if (state.panel?.phase === "lobby") {
        controls.append(
          button("开始跑团", () => sendPanelAction("phase.set", { phase: "play" }), "primary"),
        );
      } else if (state.panel?.phase === "play") {
        controls.append(
          button("返回准备阶段", () => sendPanelAction("phase.set", { phase: "lobby" })),
        );
      }
    }
    root.append(controls);
  }

  function renderModulePanel() {
    const root = $("#module-panel");
    const current = state.panel?.current_module;
    const modules = asList(state.panel?.modules, "modules", "items");
    const dm = ["owner", "dm"].includes(state.membership?.role);
    root.replaceChildren();
    if (current) {
      const scene = current.scene || current;
      root.append(
        text("p", scene.title || scene.name || current.module_id || "已激活模组", "panel-title"),
        text(
          "p",
          scene.summary || scene.description || "当前进度由 D&D MCP 管理。",
          "muted",
        ),
      );
    } else {
      root.append(text("p", "尚未激活模组", "muted"));
    }
    for (const module of modules.slice(0, 5)) {
      root.append(
        text(
          "p",
          `${module.title || module.name || module.id} ${module.active ? "· 已激活" : ""}`,
          "module-row",
        ),
      );
    }
    if (dm && current) {
      const controls = text("div", "", "panel-actions");
      controls.append(
        button(
          "推进模组",
          () => {
            const intent = prompt("描述要推进的场景、事件或进度");
            if (intent) sendPanelAction("play.intent", { intent });
          },
          "primary",
        ),
      );
      root.append(controls);
    }
  }

  function initialize() {
    $$('[data-room-mode]').forEach((control) => {
      control.onclick = () => applyRoomMode(control.dataset.roomMode);
    });
    $$('[data-composer-prompt]').forEach((control) => {
      control.onclick = () => timelineController.insertSuggestion(control.dataset.composerPrompt);
    });
    timelineController.initialize();

    $("#actor-binding-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const actorId = form.get("actor_id");
      const body = {
        user_id: form.get("user_id"),
        can_control: form.has("can_control"),
        can_view_private: form.has("can_view_private"),
      };
      try {
        await api(
          `/api/campaigns/${state.campaign.id}/actors/${encodeURIComponent(actorId)}/binding`,
          { method: "PUT", body: JSON.stringify(body) },
        );
        toast("Actor 权限已由 MCP 确认");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#back-campaigns").onclick = leaveRoom;
    $("#message-form").onsubmit = async (event) => {
      event.preventDefault();
      const field = event.target.elements.content;
      const content = field.value.trim();
      const mode = event.submitter?.value || "action";
      const audience = event.target.elements.audience.value;
      if (!content) return;
      const structuredPayload = mode === "action" ? actionContextPayload() : {};
      field.value = "";
      for (const control of event.target.querySelectorAll("button")) control.disabled = true;
      try {
        const result = await api(`/api/campaigns/${state.campaign.id}/room/messages`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            content,
            mode,
            audience,
            structured_payload: structuredPayload,
            base_revision:
              mode === "action"
                ? state.panel?.revision ?? state.campaign?.mcp_revision ?? null
                : null,
          }),
        });
        $$(".suggestion-row").forEach((row) => row.remove());
        timelineController.updateMessage(result.message);
        if (result.agent_message) timelineController.updateMessage(result.agent_message);
        if (mode === "action") {
          state.selectedTargetId = null;
          state.gridDestination = null;
          characterController.renderActionContext();
          await refreshPanel();
        }
        loadUsage();
      } catch (error) {
        field.value = content;
        toast(`发送失败：${error.message}`);
      } finally {
        for (const control of event.target.querySelectorAll("button")) control.disabled = false;
        field.focus();
      }
    };

    $$(".panel-tabs button").forEach((tab) => {
      tab.onclick = () => {
        $$(".panel-tabs button").forEach((item) =>
          item.classList.toggle("active", item === tab),
        );
        $$(".game-panel").forEach((panel) => {
          panel.hidden = panel.id !== `panel-${tab.dataset.panel}`;
        });
        $("#dm-tools").hidden =
          tab.dataset.panel !== "members" ||
          !["owner", "dm"].includes(state.membership?.role);
      };
    });
    $("#open-module-studio").onclick = openModuleStudio;
    $("#create-invite").onclick = async () => {
      const value = await api(`/api/campaigns/${state.campaign.id}/invites`, {
        method: "POST",
        body: JSON.stringify({ mode: "request" }),
      });
      $("#invite-result").textContent = `邀请代码（仅显示一次）：${value.token}`;
    };

    characterController.initialize();
    combatGridController.initialize();
  }

  async function refreshRuntime() {
    return refreshPanel();
  }

  return { initialize, openCampaign, refreshRuntime };
}
