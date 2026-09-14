import { api } from "/assets/api/client.js";
import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { state } from "/assets/state/store.js";

const moduleSteps = [
  "idea",
  "outline_ready",
  "generating",
  "draft_review",
  "ready_to_finalize",
  "compiled",
];
const moduleLabels = {
  idea: "创意",
  outlining: "提纲生成中",
  outline_ready: "提纲已就绪",
  generating: "正文生成中",
  draft_review: "审校",
  ready_to_finalize: "可定稿",
  finalizing: "编译中",
  compiled: "已编译",
  failed: "失败",
  canceled: "已取消",
};

export function createModuleStudioController() {
  let moduleViewGeneration = 0;
  let moduleConnectionGeneration = 0;
  let moduleReconnectAttempts = 0;
  let moduleReconnectTimer = null;
  let moduleStreamStatus = "idle";
  let moduleStreamTerminal = false;
  let renderGeneration = 0;

  function isCurrentModule(moduleId, viewGeneration = moduleViewGeneration) {
    return viewGeneration === moduleViewGeneration && state.module?.id === moduleId;
  }

  function setModuleStreamStatus(status, message = "") {
    moduleStreamStatus = status;
    if (message) toast(message);
    if (state.module && Array.isArray(state.moduleRuns)) renderModuleActions();
  }

  async function loadModules() {
    state.modules = await api("/api/modules");
    const root = $("#module-list");
    root.hidden = false;
    $("#module-detail").hidden = true;
    root.replaceChildren();
    for (const module of state.modules) {
      const card = text("article", "", "card module-card");
      card.append(
        text(
          "p",
          `${moduleLabels[module.status] || module.status} · D&D ${module.edition}`,
          "eyebrow",
        ),
        text("h3", module.title),
        text("p", module.brief, "muted"),
        text("p", `${module.version} · ${module.used_tokens.toLocaleString()} tokens`, "meta"),
        button("继续创作", () => openModule(module), "primary"),
      );
      root.append(card);
    }
  }

  async function openModule(module) {
    const viewGeneration = ++moduleViewGeneration;
    const loaded = await api(`/api/modules/${module.id}`);
    if (viewGeneration !== moduleViewGeneration) return;
    state.module = loaded;
    $("#module-list").hidden = true;
    $("#module-form").hidden = true;
    $("#module-detail").hidden = false;
    await renderModule(null, viewGeneration);
    watchModule();
  }

  function watchModule({ reconnect = false, viewGeneration = moduleViewGeneration } = {}) {
    const moduleId = state.module?.id;
    if (!moduleId || !isCurrentModule(moduleId, viewGeneration)) return;
    if (moduleReconnectTimer) {
      clearTimeout(moduleReconnectTimer);
      moduleReconnectTimer = null;
    }
    if (!reconnect) moduleReconnectAttempts = 0;
    if (state.moduleEvents) state.moduleEvents.close();
    const connectionGeneration = ++moduleConnectionGeneration;
    moduleStreamTerminal = false;
    setModuleStreamStatus(reconnect ? "reconnecting" : "connecting");
    const source = new EventSource(`/api/modules/${moduleId}/events`);
    state.moduleEvents = source;
    source.addEventListener("module", (event) => {
      if (
        !isCurrentModule(moduleId, viewGeneration) ||
        connectionGeneration !== moduleConnectionGeneration
      ) {
        source.close();
        return;
      }
      let data;
      try {
        data = JSON.parse(event.data || "{}");
      } catch {
        return;
      }
      if (!data.project) return;
      state.module = data.project;
      moduleStreamTerminal = Boolean(
        data.run && ["succeeded", "failed", "canceled"].includes(data.run.status),
      );
      renderModule(data.run, viewGeneration).catch(() => {});
    });
    source.onopen = () => {
      if (
        !isCurrentModule(moduleId, viewGeneration) ||
        connectionGeneration !== moduleConnectionGeneration
      ) {
        source.close();
        return;
      }
      setModuleStreamStatus("online");
    };
    let errorHandled = false;
    source.onerror = async () => {
      if (errorHandled) return;
      errorHandled = true;
      source.close();
      if (state.moduleEvents === source) state.moduleEvents = null;
      if (
        !isCurrentModule(moduleId, viewGeneration) ||
        connectionGeneration !== moduleConnectionGeneration ||
        moduleStreamTerminal
      ) {
        return;
      }
      setModuleStreamStatus("reconnecting", "模组任务流已断开，正在重新连接");
      try {
        const latest = await api(`/api/modules/${moduleId}`);
        if (
          !isCurrentModule(moduleId, viewGeneration) ||
          connectionGeneration !== moduleConnectionGeneration
        ) {
          return;
        }
        state.module = latest;
        await renderModule(null, viewGeneration);
        if (moduleStreamTerminal) {
          setModuleStreamStatus("online");
          return;
        }
        if (
          !isCurrentModule(moduleId, viewGeneration) ||
          connectionGeneration !== moduleConnectionGeneration
        ) {
          return;
        }
      } catch {
        // The scheduled reconnect will retry the state refresh as well.
      }
      scheduleModuleReconnect(moduleId, viewGeneration);
    };
  }

  function scheduleModuleReconnect(moduleId, viewGeneration) {
    if (
      moduleReconnectTimer ||
      moduleStreamTerminal ||
      !isCurrentModule(moduleId, viewGeneration)
    ) {
      return;
    }
    const delays = [1000, 2000, 5000, 10000, 30000];
    if (moduleReconnectAttempts >= delays.length) {
      setModuleStreamStatus("failed", "模组任务流连接失败，请点击重试连接");
      return;
    }
    const delay = delays[moduleReconnectAttempts++];
    moduleReconnectTimer = setTimeout(() => {
      moduleReconnectTimer = null;
      if (isCurrentModule(moduleId, viewGeneration)) {
        watchModule({ reconnect: true, viewGeneration });
      }
    }, delay);
  }

  async function renderModule(latestRun = null, expectedViewGeneration = moduleViewGeneration) {
    if (!state.module || expectedViewGeneration !== moduleViewGeneration) return;
    const module = state.module;
    const moduleId = module.id;
    const currentRender = ++renderGeneration;
    const isCurrentRender = () =>
      currentRender === renderGeneration && isCurrentModule(moduleId, expectedViewGeneration);
    $("#module-state").textContent =
      `${moduleLabels[module.status] || module.status} · D&D ${module.edition}`;
    $("#module-title").textContent = module.title;
    $("#module-brief").textContent = module.brief;
    $("#module-budget").textContent =
      `${module.used_tokens.toLocaleString()} / ${module.budget_tokens.toLocaleString()} tokens`;
    const timeline = $("#module-timeline");
    timeline.replaceChildren(
      ...moduleSteps.map((step) => {
        const element = text("span", moduleLabels[step], "timeline-step");
        const current = moduleSteps.indexOf(module.status);
        const target = moduleSteps.indexOf(step);
        if (target >= 0 && current >= target) element.classList.add("done");
        if (step === module.status) element.classList.add("current");
        return element;
      }),
    );
    $("#module-outline").textContent = Object.keys(module.outline || {}).length
      ? JSON.stringify(module.outline, null, 2)
      : "等待生成提纲";
    const review = $("#module-review");
    review.replaceChildren();
    if (module.review?.summary) {
      review.append(
        text(
          "p",
          `${module.review.approved ? "通过" : "需修订"} · ${module.review.summary}`,
          module.review.approved ? "success" : "error",
        ),
      );
      for (const finding of module.review.findings || []) {
        review.append(
          text("p", `${finding.severity || "info"}: ${finding.message || ""}`, "muted"),
        );
      }
    }
    await Promise.all([
      renderModuleSources(moduleId, expectedViewGeneration, currentRender),
      renderModuleRuns(latestRun, moduleId, expectedViewGeneration, currentRender),
    ]);
    if (!isCurrentRender()) return;
    renderModuleActions();
    renderInstallPublish();
  }

  async function renderModuleSources(moduleId, viewGeneration, currentRender) {
    const items = await api(`/api/modules/${moduleId}/sources`);
    if (
      currentRender !== renderGeneration ||
      !isCurrentModule(moduleId, viewGeneration)
    ) {
      return;
    }
    const root = $("#module-sources");
    root.replaceChildren();
    for (const item of items) {
      root.append(
        text(
          "p",
          `v${item.generation} · ${item.name} · ${item.rights_basis}`,
          "small muted",
        ),
      );
    }
    const latest = items[0];
    const kind = $("#module-publish-form select[name=source_kind]");
    const provenance = $("#module-publish-form textarea[name=provenance]");
    if (latest && kind) {
      kind.value = latest.rights_basis === "open_licensed" ? "open_licensed" : "original";
      if (provenance && !provenance.value) {
        provenance.value = latest.attribution || "原创内容，由账号持有人确认发布权利。";
      }
    }
  }

  async function renderModuleRuns(latestRun, moduleId, viewGeneration, currentRender) {
    const runs = await api(`/api/modules/${moduleId}/runs`);
    if (
      currentRender !== renderGeneration ||
      !isCurrentModule(moduleId, viewGeneration)
    ) {
      return;
    }
    state.moduleRuns = runs;
    const root = $("#module-runs");
    root.replaceChildren();
    for (const run of state.moduleRuns) {
      const row = text("div", "", "review-row");
      row.append(
        text(
          "span",
          `${run.run_type} · ${run.status} · ${run.prompt_tokens + run.completion_tokens} tokens`,
        ),
      );
      if (["queued", "running"].includes(run.status)) {
        row.append(button("取消", () => cancelModuleRun(run)));
      }
      if (["failed", "canceled"].includes(run.status)) {
        row.append(button("重试", () => retryModuleRun(run)));
      }
      if (run.error) row.append(text("small", run.error, "error"));
      root.append(row);
    }
    const newestRun = state.moduleRuns[0];
    if (newestRun && ["succeeded", "failed", "canceled"].includes(newestRun.status)) {
      moduleStreamTerminal = true;
    }
    if (latestRun && ["succeeded", "failed", "canceled"].includes(latestRun.status)) {
      toast(
        latestRun.status === "succeeded"
          ? "模组任务已完成"
          : latestRun.error || "模组任务未完成",
      );
    }
  }

  function renderModuleActions() {
    const root = $("#module-actions");
    const module = state.module;
    const active = (state.moduleRuns || []).some((run) =>
      ["queued", "running"].includes(run.status),
    );
    root.replaceChildren();
    if (moduleStreamStatus === "reconnecting") {
      root.append(text("span", "任务流已断开，正在重连", "muted"));
    }
    if (moduleStreamStatus === "failed") {
      root.append(
        text("span", "任务流连接失败", "error"),
        button("重试连接", () => watchModule()),
      );
    }
    if (active) {
      root.append(text("span", "任务正在后台执行，可离开此页面", "muted"));
      return;
    }
    const add = (label, action, klass = "") =>
      root.append(button(label, () => queueModuleAction(action), klass));
    if (["idea", "failed", "canceled"].includes(module.status)) {
      add("生成提纲", "outline", "primary");
    }
    if (module.status === "outline_ready" && !module.specification.outline_approved) {
      root.append(
        button("批准提纲", () => decideOutline(true), "primary"),
        button("退回修改", () => decideOutline(false)),
      );
    }
    if (module.status === "outline_ready" && module.specification.outline_approved) {
      add("生成完整模组", "generate", "primary");
    }
    if (module.status === "draft_review") add("Agent 证据审校", "review", "primary");
    if (["draft_review", "ready_to_finalize"].includes(module.status)) {
      add("按意见修订", "revise");
    }
    if (module.status === "ready_to_finalize") add("确认并编译", "finalize", "primary");
    if (module.status === "compiled") root.append(button("创建新版本", createModuleVersion));
  }

  async function queueModuleAction(action) {
    const moduleId = state.module.id;
    const viewGeneration = moduleViewGeneration;
    const instruction = $("#module-instruction").value;
    let body = { instruction };
    if (action === "finalize") {
      body = {
        confirmed: true,
        note: instruction || "作者确认该版本已完成 Agent 证据审校，可以编译。",
        version: state.module.version,
      };
    }
    try {
      await api(`/api/modules/${moduleId}/${action}`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      });
      if (!isCurrentModule(moduleId, viewGeneration)) return;
      const updated = await api(`/api/modules/${moduleId}`);
      if (!isCurrentModule(moduleId, viewGeneration)) return;
      state.module = updated;
      await renderModule(null, viewGeneration);
      watchModule({ viewGeneration });
    } catch (error) {
      toast(error.message);
    }
  }

  async function createModuleVersion() {
    const moduleId = state.module.id;
    const viewGeneration = moduleViewGeneration;
    const version = prompt("新版本号", state.module.version);
    if (!version || version === state.module.version) return;
    try {
      await api(`/api/modules/${moduleId}/revise`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          instruction:
            $("#module-instruction").value ||
            "Create the next release from the compiled module.",
          version,
        }),
      });
      if (!isCurrentModule(moduleId, viewGeneration)) return;
      renderModule(null, viewGeneration);
      watchModule({ viewGeneration });
    } catch (error) {
      toast(error.message);
    }
  }

  async function decideOutline(approved) {
    const moduleId = state.module.id;
    const viewGeneration = moduleViewGeneration;
    try {
      const updated = await api(`/api/modules/${moduleId}/outline-decision`, {
        method: "POST",
        body: JSON.stringify({
          approved,
          feedback: $("#module-instruction").value,
        }),
      });
      if (!isCurrentModule(moduleId, viewGeneration)) return;
      state.module = updated;
      await renderModule(null, viewGeneration);
    } catch (error) {
      toast(error.message);
    }
  }

  async function cancelModuleRun(run) {
    const moduleId = state.module.id;
    const viewGeneration = moduleViewGeneration;
    await api(`/api/modules/${moduleId}/runs/${run.id}/cancel`, { method: "POST" });
    if (!isCurrentModule(moduleId, viewGeneration)) return;
    const updated = await api(`/api/modules/${moduleId}`);
    if (!isCurrentModule(moduleId, viewGeneration)) return;
    state.module = updated;
    renderModule(null, viewGeneration);
  }

  async function retryModuleRun(run) {
    const moduleId = state.module.id;
    const viewGeneration = moduleViewGeneration;
    await api(`/api/modules/${moduleId}/runs/${run.id}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    if (!isCurrentModule(moduleId, viewGeneration)) return;
    renderModule(null, viewGeneration);
    watchModule({ viewGeneration });
  }

  function renderInstallPublish() {
    const compiled = state.module.status === "compiled";
    $("#module-install-form").hidden = !compiled;
    $("#module-publish-form").hidden = !compiled || Boolean(state.module.published_release_id);
    const select = $("#module-install-form select");
    select.replaceChildren(
      ...state.campaigns.map((campaign) => {
        const option = text("option", campaign.name);
        option.value = campaign.id;
        return option;
      }),
    );
  }

  function initialize() {
    $("#new-module").onclick = () => {
      $("#module-form").hidden = false;
    };
    $("#cancel-module").onclick = () => {
      $("#module-form").hidden = true;
    };

    $("#module-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const body = Object.fromEntries(form);
      for (const key of ["starting_level", "ending_level", "party_size", "session_hours"]) {
        body[key] = Number(body[key]);
      }
      body.locale = "zh-CN";
      try {
        const created = await api("/api/modules", {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify(body),
        });
        event.target.reset();
        event.target.hidden = true;
        await openModule(created);
      } catch (error) {
        toast(error.message);
      }
    };

    $("#close-module").onclick = () => {
      moduleViewGeneration += 1;
      moduleConnectionGeneration += 1;
      if (moduleReconnectTimer) clearTimeout(moduleReconnectTimer);
      moduleReconnectTimer = null;
      if (state.moduleEvents) state.moduleEvents.close();
      state.moduleEvents = null;
      state.module = null;
      state.moduleRuns = [];
      moduleStreamStatus = "idle";
      moduleStreamTerminal = false;
      loadModules();
    };

    $("#module-source-form").onsubmit = async (event) => {
      event.preventDefault();
      const moduleId = state.module?.id;
      const viewGeneration = moduleViewGeneration;
      if (!moduleId) return;
      try {
        await api(`/api/modules/${moduleId}/sources`, {
          method: "POST",
          body: new FormData(event.target),
        });
        event.target.reset();
        if (!isCurrentModule(moduleId, viewGeneration)) return;
        const updated = await api(`/api/modules/${moduleId}`);
        if (!isCurrentModule(moduleId, viewGeneration)) return;
        state.module = updated;
        await renderModule(null, viewGeneration);
        toast("来源资料已安全保存");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#module-install-form").onsubmit = async (event) => {
      event.preventDefault();
      const moduleId = state.module?.id;
      const viewGeneration = moduleViewGeneration;
      if (!moduleId) return;
      const form = new FormData(event.target);
      try {
        await api(`/api/modules/${moduleId}/install`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            campaign_id: form.get("campaign_id"),
            activate: form.has("activate"),
          }),
        });
        if (!isCurrentModule(moduleId, viewGeneration)) return;
        renderModule(null, viewGeneration);
        watchModule({ viewGeneration });
        toast("安装任务已提交");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#module-publish-form").onsubmit = async (event) => {
      event.preventDefault();
      const moduleId = state.module?.id;
      const viewGeneration = moduleViewGeneration;
      if (!moduleId) return;
      const form = new FormData(event.target);
      try {
        await api(`/api/modules/${moduleId}/publish`, {
          method: "POST",
          body: JSON.stringify({
            visibility: "public",
            license_code: form.get("license_code"),
            rights_attested: form.has("rights_attested"),
            source_kind: form.get("source_kind"),
            provenance: { author_statement: form.get("provenance") },
            summary: form.get("summary"),
            tags: ["dnd5e", "module"],
            changelog: "Module Studio release",
          }),
        });
        if (!isCurrentModule(moduleId, viewGeneration)) return;
        const updated = await api(`/api/modules/${moduleId}`);
        if (!isCurrentModule(moduleId, viewGeneration)) return;
        state.module = updated;
        renderModule(null, viewGeneration);
        toast("已提交平台审核");
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return { initialize, loadModules };
}
