import { api } from "/assets/api/client.js";
import { $, $$, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { characters } from "/assets/room/model.js";
import { state } from "/assets/state/store.js";

export function createRoomTimelineController({
  refreshPanel,
  loadUsage,
  loadCampaignIdentities,
  leaveRoom,
}) {
  function messageRole(message) {
    if (message.sender_type === "agent") return "agent";
    if (message.sender_type === "system") return "system";
    return message.sender_user_id === state.user.id ? "user" : "other";
  }

  function activePendingChoiceIds() {
    const latest = new Map();
    for (const message of state.roomMessages.values()) {
      for (const block of message.structured_payload?.blocks || []) {
        const presentation = block.presentation;
        if (block.type !== "resolution_ref" || !presentation) continue;
        const thread = presentation.thread_id || block.resolution_id;
        const sequence = Number(presentation.event_sequence || 1);
        const prior = latest.get(thread);
        if (!prior || sequence >= prior.sequence) {
          latest.set(thread, {
            sequence,
            pending: presentation.pending_choice,
            status: presentation.status,
          });
        }
      }
    }
    return new Set(
      [...latest.values()]
        .filter(
          (item) => ["pending", "choice"].includes(item.status) && item.pending?.id,
        )
        .map((item) => String(item.pending.id)),
    );
  }

  function suggestionCurrent(suggestion) {
    const valid = suggestion?.valid_for || {};
    const phase = state.panel?.phase;
    const currentRevision =
      state.panel?.revision ?? state.panel?.campaign?.revision ?? state.campaign?.mcp_revision;
    if (valid.expired) return false;
    if (valid.phase && phase && valid.phase !== phase) return false;
    if (
      valid.revision !== null &&
      valid.revision !== undefined &&
      currentRevision !== null &&
      currentRevision !== undefined &&
      Number(valid.revision) !== Number(currentRevision)
    ) {
      return false;
    }
    if (valid.actor_ref && valid.actor_ref !== state.actingActorId) return false;
    if (
      valid.actor_ref &&
      valid.actor_revision !== null &&
      valid.actor_revision !== undefined
    ) {
      const actor = characters().find((item) => item.id === valid.actor_ref);
      if (!actor || Number(actor.revision) !== Number(valid.actor_revision)) return false;
    }
    if (
      valid.pending_choice_id &&
      !activePendingChoiceIds().has(String(valid.pending_choice_id))
    ) {
      return false;
    }
    return true;
  }

  function insertSuggestion(value) {
    const field = $("#message-form textarea");
    const addition = String(value || "").trim();
    if (!field || !addition) return;
    field.value = field.value.trim() ? `${field.value.trim()} ${addition}` : addition;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  }

  function renderPerformance(block) {
    const card = text("section", "", "performance-card");
    const speaker = block.speaker || {};
    const head = text("header", "", "performance-head");
    const avatar = text(
      "span",
      String(speaker.label || "?")
        .trim()
        .slice(0, 1)
        .toUpperCase(),
      "performance-avatar",
    );
    head.append(avatar, text("strong", speaker.label || "未知角色"));
    card.append(head);
    for (const beat of block.beats || []) {
      const klass = beat.type === "speech" ? "performance-speech" : "performance-action";
      const line = text(beat.type === "speech" ? "blockquote" : "p", beat.text || "", klass);
      if (beat.type === "speech") {
        line.setAttribute("aria-label", `${speaker.label || "角色"}说：${beat.text || ""}`);
      }
      card.append(line);
    }
    return card;
  }

  function renderResolution(block) {
    const presentation = block.presentation || {};
    const sequence = Number(presentation.event_sequence || 1);
    const eventKey = `${block.resolution_id}:${sequence}`;
    const seen = state.animatedResolutions.has(eventKey);
    const row = text(
      "section",
      "",
      `resolution-reference resolution-${presentation.status || "unknown"}`,
    );
    const statusNames = {
      pending: "等待选择",
      choice: "等待选择",
      settled: "已结算",
      aborted: "已中止",
      failed: "失败",
    };
    const status = statusNames[presentation.status] || "权威判定";
    const rolls = Array.isArray(presentation.rolls) ? presentation.rolls : [];
    const outcome = presentation.outcome || {};
    const summary = [];
    row.dataset.resolutionId = block.resolution_id;
    row.dataset.eventSequence = String(sequence);
    row.setAttribute("role", "status");
    if (!seen && !state.hydratingRoom) row.classList.add("resolution-enter");
    state.animatedResolutions.add(eventKey);
    row.append(
      text("span", presentation.system_id === "coc7e" ? "◉" : "⚄", "resolution-die"),
      text("strong", status),
    );
    for (const roll of rolls) {
      const dice = Array.isArray(roll.dice) ? roll.dice.join(", ") : "—";
      const line = text("div", "", "resolution-roll");
      line.append(
        text("span", `${roll.expression || "骰点"} [${dice}]`),
        text("strong", `＝ ${roll.total}`),
      );
      row.append(line);
      summary.push(`${roll.expression || "骰点"}，骰值 ${dice}，合计 ${roll.total}`);
    }
    const outcomeParts = [];
    for (const key of [
      "success_level",
      "outcome",
      "success",
      "hit",
      "critical",
      "fumble",
      "damage",
      "san_loss",
    ]) {
      if (outcome[key] !== undefined && outcome[key] !== null) {
        outcomeParts.push(`${key}: ${String(outcome[key])}`);
      }
    }
    if (outcomeParts.length) {
      row.append(text("small", outcomeParts.join(" · "), "resolution-outcome"));
    }
    const pending = presentation.pending_choice;
    const actions = Array.isArray(pending?.available_actions) ? pending.available_actions : [];
    if (actions.length) {
      const choices = text("div", "", "resolution-choices");
      choices.setAttribute("aria-label", "可选择的后续结算");
      for (const action of actions) {
        choices.append(button(action, () => insertSuggestion(action), "resolution-choice"));
      }
      row.append(choices);
    }
    row.setAttribute(
      "aria-label",
      `${status}。${summary.join("；")}。${outcomeParts.join("；")}`,
    );
    return row;
  }

  function renderPresentation(payload, fallback) {
    const body = text("div", "", "message-body presentation-body");
    const blocks = Array.isArray(payload?.blocks) ? payload.blocks : [];
    if (!blocks.length) {
      body.append(text("div", fallback || ""));
      return body;
    }
    for (const block of blocks) {
      if (block.type === "narration") {
        body.append(text("p", block.text || "", "narration-block"));
      } else if (block.type === "performance") {
        body.append(renderPerformance(block));
      } else if (block.type === "resolution_ref") {
        body.append(renderResolution(block));
      } else if (block.type === "prompt") {
        body.append(text("p", block.text || "", "turn-prompt"));
      }
    }
    return body;
  }

  function renderSuggestions(payload) {
    const suggestions = Array.isArray(payload?.suggestions) ? payload.suggestions : [];
    if (!suggestions.length) return null;
    const root = text("div", "", "suggestion-row");
    root.setAttribute("aria-label", "快捷输入建议");
    for (const item of suggestions) {
      const control = button(
        item.text,
        () => insertSuggestion(item.text),
        "suggestion-chip",
      );
      control.disabled = !suggestionCurrent(item);
      control.title = control.disabled
        ? "战役状态已变化，此建议已失效"
        : "加入输入框，可继续编辑";
      control.dataset.runId = item.valid_for?.run_id || "";
      control.dataset.revision = item.valid_for?.revision ?? "";
      control.dataset.phase = item.valid_for?.phase || "";
      control.dataset.actorRef = item.valid_for?.actor_ref || "";
      control.dataset.actorRevision = item.valid_for?.actor_revision ?? "";
      control.dataset.pendingChoiceId = item.valid_for?.pending_choice_id || "";
      root.append(control);
    }
    return root;
  }

  function refreshSuggestionValidity() {
    for (const control of $$(".suggestion-chip")) {
      const valid = {
        valid_for: {
          run_id: control.dataset.runId,
          revision: control.dataset.revision ? Number(control.dataset.revision) : null,
          phase: control.dataset.phase || null,
          actor_ref: control.dataset.actorRef || null,
          actor_revision: control.dataset.actorRevision
            ? Number(control.dataset.actorRevision)
            : null,
          pending_choice_id: control.dataset.pendingChoiceId || null,
        },
      };
      const current = suggestionCurrent(valid);
      control.disabled = !current;
      control.title = current
        ? "加入输入框，可继续编辑"
        : "战役状态已变化，此建议已失效";
    }
  }

  function timelineNearBottom(timeline) {
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 72;
  }

  function renderMessage(message) {
    if (!message || state.roomMessages.has(message.id)) return;
    state.roomMessages.set(message.id, message);
    const timeline = $("#messages");
    const follow = state.hydratingRoom || timelineNearBottom(timeline);
    const bubble = text(
      "article",
      "",
      `bubble ${messageRole(message)} message-${message.message_type}`,
    );
    bubble.dataset.messageId = message.id;
    bubble.dataset.sequence = message.sequence;
    const head = text(
      "header",
      `${message.sender_display_name} · ${new Date(message.created_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })}`,
      "message-head",
    );
    const payload = message.structured_payload || {};
    const body =
      payload.schema === "sagasmith.room-message/v1"
        ? renderPresentation(payload, message.content)
        : text("div", message.content, "message-body");
    bubble.append(head, body);
    const suggestions = renderSuggestions(payload);
    if (suggestions) bubble.append(suggestions);
    if (message.audience !== "public") {
      bubble.append(
        text("small", message.audience === "dm" ? "仅 DM 可见" : "私密消息", "audience-badge"),
      );
    }
    if (message.status === "processing") {
      bubble.append(text("small", "SagaSmith 正在结算…", "message-status"));
    }
    if (message.status === "failed") {
      bubble.append(text("small", "行动未完成，可重新发送", "error"));
    }
    if (message.structured_payload?.panel_action) {
      bubble.append(text("small", message.structured_payload.panel_action, "receipt-badge"));
    }
    const next = [...timeline.children].find(
      (item) => Number(item.dataset.sequence) > Number(message.sequence),
    );
    timeline.insertBefore(bubble, next || null);
    if (follow) {
      timeline.scrollTop = timeline.scrollHeight;
      $("#new-messages").hidden = true;
    } else {
      $("#new-messages").hidden = false;
    }
  }

  function updateMessage(message) {
    if (!message) return;
    const old = $(`[data-message-id="${message.id}"]`);
    if (old) {
      old.remove();
      state.roomMessages.delete(message.id);
    }
    renderMessage(message);
    refreshSuggestionValidity();
  }

  async function loadRoomSnapshot() {
    const snapshot = await api(
      `/api/campaigns/${state.campaign.id}/room/snapshot?limit=200`,
    );
    state.room = snapshot.room;
    state.roomEventCursor = snapshot.event_cursor;
    $("#messages").replaceChildren();
    state.roomMessages = new Map();
    state.hydratingRoom = true;
    for (const item of snapshot.messages) renderMessage(item);
    state.hydratingRoom = false;
    $("#messages").scrollTop = $("#messages").scrollHeight;
    $("#new-messages").hidden = true;
  }

  function connectRoomEvents() {
    if (state.roomEvents) state.roomEvents.close();
    const campaignId = state.campaign.id;
    const source = new EventSource(
      `/api/campaigns/${campaignId}/room/events?after=${state.roomEventCursor}`,
    );
    const activityNames = {
      reviewing_rules: "正在查阅规则",
      checking_range: "正在确认距离",
      resolving_roll: "正在结算骰点",
      settling_save: "正在结算豁免",
      awaiting_choice: "等待选择",
      updating_state: "正在更新状态",
      preparing_narration: "正在准备叙述",
    };
    state.roomEvents = source;
    source.onopen = () => {
      $("#room-sync").textContent = "实时同步";
      $("#room-sync").classList.add("online");
    };
    source.onerror = () => {
      $("#room-sync").textContent = "正在重连";
      $("#room-sync").classList.remove("online");
    };
    const receive = (event) => {
      if (state.campaign?.id !== campaignId) return;
      state.roomEventCursor = Math.max(
        state.roomEventCursor,
        Number(event.lastEventId || 0),
      );
      const value = JSON.parse(event.data || "{}");
      if (value.message) updateMessage(value.message);
      return value;
    };
    source.addEventListener("message.created", receive);
    source.addEventListener("message.updated", receive);
    source.addEventListener("agent.started", (event) => {
      $("#room-sync").textContent = "Agent 正在处理";
      receive(event);
    });
    source.addEventListener("room.activity", (event) => {
      const value = receive(event) || {};
      $("#room-sync").textContent =
        value.state === "started"
          ? activityNames[value.code] || "Agent 正在处理"
          : "Agent 正在处理";
    });
    source.addEventListener("agent.completed", (event) => {
      receive(event);
      refreshPanel();
      loadUsage();
    });
    source.addEventListener("agent.failed", receive);
    source.addEventListener("state.changed", (event) => {
      receive(event);
      refreshPanel().then(refreshSuggestionValidity);
    });
    source.addEventListener("host.changed", (event) => {
      receive(event);
      const value = JSON.parse(event.data || "{}");
      if (state.room) {
        state.room.host_identity_assignment_id = value.identity_assignment_id || null;
      }
      loadCampaignIdentities();
    });
    source.addEventListener("access.revoked", () => {
      source.close();
      toast("你的战役访问已被撤销");
      leaveRoom();
    });
  }

  function initialize() {
    $("#messages").addEventListener("scroll", () => {
      if (timelineNearBottom($("#messages"))) $("#new-messages").hidden = true;
    });
    $("#new-messages").onclick = () => {
      $("#messages").scrollTo({ top: $("#messages").scrollHeight, behavior: "smooth" });
      $("#new-messages").hidden = true;
    };
  }

  return {
    connectRoomEvents,
    initialize,
    insertSuggestion,
    loadRoomSnapshot,
    refreshSuggestionValidity,
    updateMessage,
  };
}
