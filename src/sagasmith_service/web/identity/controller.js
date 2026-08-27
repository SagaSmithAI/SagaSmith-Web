import { api } from "/assets/api/client.js";
import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { state } from "/assets/state/store.js";

export function createIdentityController() {
  async function loadSoulOptions() {
    const souls = await api("/api/community/artifacts?artifact_type=soul");
    const options = [];
    for (const soul of souls) {
      for (const release of await api(`/api/community/artifacts/${soul.id}/releases`)) {
        if (release.status === "published") {
          options.push({
            id: release.id,
            label: `${soul.title} · ${release.version}`,
          });
        }
      }
    }
    const select = $("#identity-soul-select");
    select.replaceChildren(
      ...options.map((item) => {
        const option = text("option", item.label);
        option.value = item.id;
        return option;
      }),
    );
  }

  async function loadIdentities() {
    await loadSoulOptions();
    state.identities = await api("/api/identities");
    state.assignments = await api("/api/identities/assignments/mine");
    const root = $("#identity-list");
    root.replaceChildren();
    for (const identity of state.identities) {
      const card = text("article", "", "card identity-card");
      card.append(
        text(
          "p",
          `${identity.identity_kind.toUpperCase()} · ${identity.system_id}`,
          "eyebrow",
        ),
        text("h3", identity.name),
        text("p", identity.bio || "暂无简介", "muted"),
        text("p", `@${identity.handle} · ${identity.availability}`, "meta"),
      );
      root.append(card);
    }
    renderAssignments();
  }

  function renderAssignments() {
    const root = $("#assignment-list");
    root.replaceChildren();
    for (const assignment of state.assignments) {
      const identity = state.identities.find((item) => item.id === assignment.identity_id);
      const campaign = state.campaigns.find((item) => item.id === assignment.campaign_id);
      const card = text("article", "", "card assignment-card");
      card.append(
        text("p", `${assignment.role.toUpperCase()} · ${assignment.status}`, "eyebrow"),
        text("h3", identity?.name || assignment.identity_id),
        text("p", campaign?.name || assignment.campaign_id, "muted"),
        text("p", assignment.memory_namespace, "small muted"),
      );
      if (
        assignment.status === "pending" &&
        identity?.owner_user_id === state.user.id
      ) {
        card.append(
          button("接受", () => decideAssignment(assignment.id, "accepted"), "primary"),
          button("拒绝", () => decideAssignment(assignment.id, "rejected")),
        );
      }
      if (assignment.status === "accepted") {
        card.append(
          button("管理战役记忆", () => manageAssignmentMemory(assignment)),
          button("撤销任职", () => revokeAssignment(assignment.id)),
        );
      }
      root.append(card);
    }
  }

  async function decideAssignment(id, decision) {
    try {
      await api(`/api/identities/assignments/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      toast(decision === "accepted" ? "已通过 D&D MCP 获得 DM 权限" : "已拒绝");
      loadIdentities();
    } catch (error) {
      toast(error.message);
    }
  }

  async function revokeAssignment(id) {
    if (!confirm("撤销 Identity 的战役权限和记忆访问？")) return;
    try {
      await api(`/api/identities/assignments/${id}`, { method: "DELETE" });
      toast("Identity 权限已撤销");
      loadIdentities();
    } catch (error) {
      toast(error.message);
    }
  }

  async function manageAssignmentMemory(assignment) {
    try {
      const memories = await api(`/api/identities/assignments/${assignment.id}/memory`);
      const key = prompt("记忆键", memories[0]?.memory_key || "current-scene");
      if (!key) return;
      const current = memories.find((item) => item.memory_key === key);
      const content = prompt("战役隔离记忆", current?.content || "");
      if (!content) return;
      await api(
        `/api/identities/assignments/${assignment.id}/memory/${encodeURIComponent(key)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            content,
            audience: "dm",
            source: "curated",
            expected_revision: current?.revision || null,
          }),
        },
      );
      toast("战役记忆已按 revision 保存");
    } catch (error) {
      toast(error.message);
    }
  }

  async function setRoomHost(assignmentId) {
    try {
      state.room = await api(`/api/campaigns/${state.campaign.id}/room/host`, {
        method: "PUT",
        body: JSON.stringify({ identity_assignment_id: assignmentId }),
      });
      await loadCampaignIdentities();
      toast(assignmentId ? "托管主持人已启用" : "已改为按发送者权限运行");
    } catch (error) {
      toast(error.message);
    }
  }

  async function loadCampaignIdentities() {
    state.assignments = await api("/api/identities/assignments/mine");
    if (!state.identities.length) state.identities = await api("/api/identities");
    const active = state.assignments.filter(
      (assignment) =>
        assignment.campaign_id === state.campaign.id && assignment.status === "accepted",
    );
    const root = $("#campaign-identities");
    const selected = state.room?.host_identity_assignment_id;
    const isOwner = state.membership?.role === "owner";
    root.replaceChildren();
    for (const assignment of active) {
      const identity = state.identities.find((item) => item.id === assignment.identity_id);
      const row = text("div", "", "review-row");
      const isSelected = selected === assignment.id;
      row.append(
        text("span", `${identity?.name || "DM Identity"}${isSelected ? " · 当前主持" : ""}`),
      );
      row.append(button("私聊", () => useIdentityConversation(assignment, identity)));
      if (isOwner && !isSelected) {
        row.append(button("设为主持", () => setRoomHost(assignment.id), "primary"));
      }
      root.append(row);
    }
    if (selected && isOwner) {
      root.append(button("停用托管主持", () => setRoomHost(null)));
    }
    if (!active.length) {
      root.append(text("p", "尚未绑定托管主持人", "muted small"));
    }
    const current = active.find((assignment) => assignment.id === selected);
    const identity =
      current && state.identities.find((item) => item.id === current.identity_id);
    $("#active-host").textContent = identity
      ? `${identity.name} 正在主持 · 独立 Agent/MCP session`
      : "共享战役时间线 · 每次行动按发送者权限结算";
  }

  async function useIdentityConversation(_assignment, identity) {
    toast(
      `${identity?.name || "DM Identity"} 已任职；共享房间中的 Identity 发言将在专用 principal 接入后启用`,
    );
  }

  async function loadIdentityInviteOptions() {
    const identities = await api("/api/identities?system_id=dnd5e&identity_kind=dm");
    const select = $("#identity-invite-select");
    select.replaceChildren(
      ...identities
        .filter((identity) => identity.availability !== "unavailable")
        .map((identity) => {
          const option = text("option", `${identity.name} · @${identity.handle}`);
          option.value = identity.id;
          return option;
        }),
    );
  }

  function initialize() {
    $("#identity-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const body = Object.fromEntries(form);
      body.memory_policy = { campaign_isolation: "required" };
      body.public_profile = {};
      try {
        await api("/api/identities", {
          method: "POST",
          body: JSON.stringify(body),
        });
        event.target.reset();
        toast("Identity 已创建");
        loadIdentities();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#identity-invite-form").onsubmit = async (event) => {
      event.preventDefault();
      const identityId = new FormData(event.target).get("identity_id");
      try {
        await api(`/api/identities/campaigns/${state.campaign.id}/invitations`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ identity_id: identityId }),
        });
        toast("Identity 邀请已发出");
        await loadCampaignIdentities();
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return {
    initialize,
    loadCampaignIdentities,
    loadIdentities,
    loadIdentityInviteOptions,
    loadSoulOptions,
  };
}
