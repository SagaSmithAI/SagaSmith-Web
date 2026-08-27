import { api } from "/assets/api/client.js";
import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { parseJson, typeNames } from "/assets/forge/shared.js";
import { state } from "/assets/state/store.js";

export function createForgeStudio({ loadPacks, loadSoulOptions, loadModeration }) {
  async function loadStudio() {
    await loadPacks();
    const mine = await api("/api/community/artifacts?mine=true");
    const root = $("#studio-list");
    root.hidden = false;
    $("#release-editor").hidden = true;
    root.replaceChildren();
    for (const artifact of mine) {
      const card = text("article", "", "card artifact-card");
      card.append(
        text("p", `${typeNames[artifact.artifact_type]} · ${artifact.status}`, "eyebrow"),
        text("h3", artifact.title),
        text("p", artifact.summary, "muted"),
        button("管理版本", () => openReleaseEditor(artifact), "primary"),
      );
      root.append(card);
    }
    await loadSoulOptions();
    if (state.user.is_admin) await loadModeration();
  }

  async function openReleaseEditor(artifact) {
    state.studioArtifact = artifact;
    $("#studio-list").hidden = true;
    $("#release-editor").hidden = false;
    $("#release-editor-title").textContent = `${artifact.title} · ${artifact.visibility}`;
    const select = $("#release-pack-select");
    select.replaceChildren();
    const empty = text("option", "无");
    empty.value = "";
    select.append(
      empty,
      ...state.packs.map((pack) => {
        const option = text("option", `${pack.title} · ${pack.kind} · ${pack.version}`);
        option.value = pack.id;
        return option;
      }),
    );
    await loadReleaseEditorList();
  }

  async function loadReleaseEditorList() {
    state.releases = await api(
      `/api/community/artifacts/${state.studioArtifact.id}/releases`,
    );
    const root = $("#release-list");
    root.replaceChildren();
    for (const release of state.releases) {
      const row = text("div", "", "release-row");
      row.append(text("span", `${release.version} · ${release.status}`));
      if (release.status === "draft") {
        row.append(button("Agent 审核", () => reviewRelease(release)));
      }
      if (release.status === "agent_reviewed") {
        row.append(button("提交平台审核", () => submitRelease(release), "primary"));
      }
      row.append(
        text("small", release.agent_review?.summary || release.moderation_notes || "", "muted"),
      );
      root.append(row);
    }
    if (state.studioArtifact.visibility === "private") {
      root.prepend(button("设为公开并确认发布权利", prepareArtifactPublication));
    }
  }

  async function prepareArtifactPublication() {
    try {
      state.studioArtifact = await api(
        `/api/community/artifacts/${state.studioArtifact.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({ visibility: "public", rights_attested: true }),
        },
      );
      toast("作品已准备公开，仍需版本审核");
      loadReleaseEditorList();
    } catch (error) {
      toast(error.message);
    }
  }

  async function reviewRelease(release) {
    try {
      await api(
        `/api/community/artifacts/${state.studioArtifact.id}/releases/${release.id}/agent-review`,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
        },
      );
      toast("Agent 审核完成");
      loadReleaseEditorList();
    } catch (error) {
      toast(error.message);
    }
  }

  async function submitRelease(release) {
    try {
      await api(
        `/api/community/artifacts/${state.studioArtifact.id}/releases/${release.id}/submit`,
        { method: "POST" },
      );
      toast("已提交平台审核");
      loadReleaseEditorList();
    } catch (error) {
      toast(error.message);
    }
  }

  function initialize() {
    $("#artifact-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const body = Object.fromEntries(form);
      body.rights_attested = form.has("rights_attested");
      body.provenance = { author_statement: "Submitted by account owner" };
      body.tags = [];
      try {
        await api("/api/community/artifacts", {
          method: "POST",
          body: JSON.stringify(body),
        });
        event.target.reset();
        toast("作品草稿已创建");
        loadStudio();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#release-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      try {
        const body = {
          version: form.get("version"),
          private_pack_id: form.get("private_pack_id") || null,
          manifest: parseJson(form.get("manifest"), "Manifest"),
          payload: parseJson(form.get("payload"), "Payload"),
          compatibility: parseJson(form.get("compatibility"), "兼容性"),
          changelog: form.get("changelog"),
          contains_private_source: form.has("contains_private_source"),
        };
        await api(`/api/community/artifacts/${state.studioArtifact.id}/releases`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        event.target.reset();
        event.target.elements.manifest.value = "{}";
        event.target.elements.payload.value = "{}";
        event.target.elements.compatibility.value = '{"edition":"2024"}';
        toast("Release 草稿已创建");
        loadReleaseEditorList();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#close-release-editor").onclick = () => {
      $("#release-editor").hidden = true;
      $("#studio-list").hidden = false;
    };
  }

  return { initialize, loadStudio };
}
