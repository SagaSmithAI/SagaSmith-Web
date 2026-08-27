import { api } from "/assets/api/client.js";
import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { typeNames } from "/assets/forge/shared.js";
import { state } from "/assets/state/store.js";

export function createForgeCatalog() {
  async function loadForge(filters = {}) {
    const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    state.artifacts = await api(`/api/community/artifacts?${query}`);
    const root = $("#forge-list");
    root.hidden = false;
    $("#artifact-detail").hidden = true;
    root.replaceChildren();
    for (const artifact of state.artifacts) {
      const card = text("article", "", "card artifact-card");
      card.append(
        text(
          "p",
          `${typeNames[artifact.artifact_type] || artifact.artifact_type} · ${artifact.system_id}`,
          "eyebrow",
        ),
        text("h3", artifact.title),
        text("p", artifact.summary || "暂无简介", "muted"),
      );
      const tags = text("div", "", "tag-row");
      for (const tag of artifact.tags) tags.append(text("span", tag, "tag"));
      card.append(
        tags,
        text(
          "p",
          `${artifact.owner_display_name} · ${artifact.license_code} · ★ ${artifact.favorite_count}`,
          "meta",
        ),
      );
      card.append(button("查看作品", () => openArtifact(artifact), "primary"));
      root.append(card);
    }
  }

  async function openArtifact(artifact) {
    state.artifact = artifact;
    state.releases = await api(`/api/community/artifacts/${artifact.id}/releases`);
    $("#forge-list").hidden = true;
    $("#artifact-detail").hidden = false;
    const summary = $("#artifact-summary");
    summary.replaceChildren(
      text("p", `${typeNames[artifact.artifact_type]} · ${artifact.system_id}`, "eyebrow"),
      text("h2", artifact.title),
      text("p", artifact.summary, "muted"),
      text(
        "p",
        `作者 ${artifact.owner_display_name} · ${artifact.license_code} · 来源 ${artifact.source_kind}`,
        "meta",
      ),
    );
    const actions = text("div", "", "actions left");
    actions.append(
      button("收藏", async () => {
        await api(`/api/community/artifacts/${artifact.id}/favorite`, { method: "PUT" });
        toast("已收藏");
      }),
    );
    if (artifact.license_code !== "ARR") {
      actions.append(button("Fork", () => forkArtifact(artifact)));
    }
    actions.append(button("举报", () => reportArtifact(artifact)));
    summary.append(actions);

    const releases = $("#artifact-releases");
    releases.replaceChildren(text("h3", "发布版本"));
    for (const release of state.releases) {
      const row = text("div", "", "release-row");
      row.append(text("span", `${release.version} · ${release.status}`));
      if (release.status === "published") {
        row.append(buildInstallControl(artifact, release));
      }
      releases.append(row);
    }
    await loadArtifactPosts();
  }

  function buildInstallControl(artifact, release) {
    const wrap = text("div", "", "install-control");
    if (["module", "rule", "character"].includes(artifact.artifact_type)) {
      const select = text("select", "");
      for (const campaign of state.campaigns) {
        const option = text("option", campaign.name);
        option.value = campaign.id;
        select.append(option);
      }
      const activate = document.createElement("input");
      activate.type = "checkbox";
      const label = text("label", " 激活", "check");
      label.prepend(activate);
      wrap.append(
        select,
        label,
        button("安装", async () => {
          try {
            await api(`/api/community/releases/${release.id}/install`, {
              method: "POST",
              headers: { "Idempotency-Key": crypto.randomUUID() },
              body: JSON.stringify({
                campaign_id: select.value,
                activate: activate.checked,
              }),
            });
            toast("已通过 D&D MCP 安装");
          } catch (error) {
            toast(error.message);
          }
        }),
      );
    } else {
      wrap.append(
        button("加入资料库", async () => {
          try {
            await api(`/api/community/releases/${release.id}/install`, {
              method: "POST",
              headers: { "Idempotency-Key": crypto.randomUUID() },
              body: JSON.stringify({}),
            });
            toast("已加入资料库");
          } catch (error) {
            toast(error.message);
          }
        }),
      );
    }
    return wrap;
  }

  async function forkArtifact(artifact) {
    const title = prompt("Fork 标题", `${artifact.title} Fork`);
    const slug = prompt("新 Slug", `${artifact.slug}-fork`);
    if (!title || !slug) return;
    try {
      await api(`/api/community/artifacts/${artifact.id}/fork`, {
        method: "POST",
        body: JSON.stringify({ title, slug }),
      });
      toast("Fork 草稿已创建");
    } catch (error) {
      toast(error.message);
    }
  }

  async function reportArtifact(artifact) {
    const details = prompt("请说明举报理由");
    if (!details) return;
    try {
      await api("/api/community/reports", {
        method: "POST",
        body: JSON.stringify({
          target_type: "artifact",
          target_id: artifact.id,
          reason: "other",
          details,
        }),
      });
      toast("举报已进入审核队列");
    } catch (error) {
      toast(error.message);
    }
  }

  async function loadArtifactPosts() {
    const posts = await api(
      `/api/community/posts?target_type=artifact&target_id=${state.artifact.id}`,
    );
    const root = $("#artifact-posts");
    root.replaceChildren();
    for (const post of posts) {
      const item = text("article", "", "post");
      item.append(
        text(
          "p",
          `${post.author_display_name} · ${post.category}${post.spoiler ? " · 剧透" : ""}`,
          "eyebrow",
        ),
        text("p", post.spoiler ? `剧透：${post.body}` : post.body),
      );
      root.append(item);
    }
  }

  function initialize() {
    $("#forge-search").onsubmit = async (event) => {
      event.preventDefault();
      await loadForge(Object.fromEntries(new FormData(event.target)));
    };

    $("#post-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const release = state.releases.find((item) => item.status === "published");
      try {
        await api("/api/community/posts", {
          method: "POST",
          body: JSON.stringify({
            target_type: "artifact",
            target_id: state.artifact.id,
            release_id: release?.id || null,
            category: form.get("category"),
            spoiler: form.has("spoiler"),
            body: form.get("body"),
            audience: "public",
          }),
        });
        event.target.reset();
        await loadArtifactPosts();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#close-artifact").onclick = () => {
      $("#artifact-detail").hidden = true;
      $("#forge-list").hidden = false;
    };
  }

  return { initialize, loadForge };
}
