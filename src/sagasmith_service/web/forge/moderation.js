import { api } from "/assets/api/client.js";
import { $, button, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";

export function createForgeModeration() {
  async function loadModeration() {
    const [releases, reports] = await Promise.all([
      api("/api/community/admin/releases"),
      api("/api/community/admin/reports"),
    ]);
    const releaseRoot = $("#moderation-releases");
    releaseRoot.replaceChildren();
    for (const release of releases) {
      const row = text("div", "", "review-row");
      row.append(
        text("span", `${release.artifact_id.slice(0, 8)} · ${release.version}`),
        button("批准", () => moderateRelease(release.id, "approved"), "primary"),
        button("拒绝", () => moderateRelease(release.id, "rejected")),
      );
      releaseRoot.append(row);
    }

    const reportRoot = $("#moderation-reports");
    reportRoot.replaceChildren();
    for (const report of reports.filter((item) => item.status === "open")) {
      const row = text("div", "", "review-row");
      row.append(
        text("span", `${report.reason} · ${report.details}`),
        button("下架/解决", () => decideReport(report.id, "resolved"), "primary"),
        button("驳回", () => decideReport(report.id, "dismissed")),
      );
      reportRoot.append(row);
    }
  }

  async function moderateRelease(id, decision) {
    const notes = prompt("审核备注") || "";
    try {
      await api(`/api/community/admin/releases/${id}/moderate`, {
        method: "POST",
        body: JSON.stringify({ decision, notes }),
      });
      loadModeration();
    } catch (error) {
      toast(error.message);
    }
  }

  async function decideReport(id, status) {
    const resolution = prompt("处理说明");
    if (!resolution) return;
    try {
      await api(`/api/community/admin/reports/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ status, resolution }),
      });
      loadModeration();
    } catch (error) {
      toast(error.message);
    }
  }

  return { loadModeration };
}
