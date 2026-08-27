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
    state.module = await api(`/api/modules/${module.id}`);
    $("#module-list").hidden = true;
    $("#module-form").hidden = true;
    $("#module-detail").hidden = false;
    await renderModule();
    watchModule();
  }

  function watchModule() {
    if (state.moduleEvents) state.moduleEvents.close();
    state.moduleEvents = new EventSource(`/api/modules/${state.module.id}/events`);
    state.moduleEvents.addEventListener("module", (event) => {
      const data = JSON.parse(event.data);
      state.module = data.project;
      renderModule(data.run).catch(() => {});
    });
    state.moduleEvents.onerror = () => {
      state.moduleEvents?.close();
      state.moduleEvents = null;
    };
  }

  async function renderModule(latestRun = null) {
    const module = state.module;
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
    await Promise.all([renderModuleSources(), renderModuleRuns(latestRun)]);
    renderModuleActions();
    renderInstallPublish();
  }

  async function renderModuleSources() {
    const items = await api(`/api/modules/${state.module.id}/sources`);
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

  async function renderModuleRuns(latestRun) {
    state.moduleRuns = await api(`/api/modules/${state.module.id}/runs`);
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
    const active = state.moduleRuns.some((run) => ["queued", "running"].includes(run.status));
    root.replaceChildren();
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
      await api(`/api/modules/${state.module.id}/${action}`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(body),
      });
      state.module = await api(`/api/modules/${state.module.id}`);
      await renderModule();
      watchModule();
    } catch (error) {
      toast(error.message);
    }
  }

  async function createModuleVersion() {
    const version = prompt("新版本号", state.module.version);
    if (!version || version === state.module.version) return;
    try {
      await api(`/api/modules/${state.module.id}/revise`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          instruction:
            $("#module-instruction").value ||
            "Create the next release from the compiled module.",
          version,
        }),
      });
      renderModule();
      watchModule();
    } catch (error) {
      toast(error.message);
    }
  }

  async function decideOutline(approved) {
    try {
      state.module = await api(`/api/modules/${state.module.id}/outline-decision`, {
        method: "POST",
        body: JSON.stringify({
          approved,
          feedback: $("#module-instruction").value,
        }),
      });
      await renderModule();
    } catch (error) {
      toast(error.message);
    }
  }

  async function cancelModuleRun(run) {
    await api(`/api/modules/${state.module.id}/runs/${run.id}/cancel`, { method: "POST" });
    state.module = await api(`/api/modules/${state.module.id}`);
    renderModule();
  }

  async function retryModuleRun(run) {
    await api(`/api/modules/${state.module.id}/runs/${run.id}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    renderModule();
    watchModule();
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
      if (state.moduleEvents) state.moduleEvents.close();
      state.module = null;
      loadModules();
    };

    $("#module-source-form").onsubmit = async (event) => {
      event.preventDefault();
      try {
        await api(`/api/modules/${state.module.id}/sources`, {
          method: "POST",
          body: new FormData(event.target),
        });
        event.target.reset();
        state.module = await api(`/api/modules/${state.module.id}`);
        await renderModule();
        toast("来源资料已安全保存");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#module-install-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      try {
        await api(`/api/modules/${state.module.id}/install`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            campaign_id: form.get("campaign_id"),
            activate: form.has("activate"),
          }),
        });
        renderModule();
        watchModule();
        toast("安装任务已提交");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#module-publish-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      try {
        await api(`/api/modules/${state.module.id}/publish`, {
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
        state.module = await api(`/api/modules/${state.module.id}`);
        renderModule();
        toast("已提交平台审核");
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return { initialize, loadModules };
}
