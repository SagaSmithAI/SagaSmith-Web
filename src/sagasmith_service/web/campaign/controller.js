import { api } from "/assets/api/client.js";
import { $, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { state } from "/assets/state/store.js";

export function createCampaignController({ openCampaign }) {
  async function loadCampaigns() {
    state.campaigns = await api("/api/campaigns");
    const list = $("#campaign-list");
    list.replaceChildren();
    for (const campaign of state.campaigns) {
      const card = text("article", "", "card campaign");
      card.append(
        text("p", campaign.system_id.toUpperCase(), "eyebrow"),
        text("h3", campaign.name),
      );
      const meta = text("div", "", "meta");
      meta.append(text("span", campaign.visibility), text("span", `rev ${campaign.mcp_revision}`));
      card.append(meta);
      card.onclick = () => openCampaign(campaign);
      list.append(card);
    }
  }

  function initialize() {
    $("#new-campaign").onclick = () => {
      $("#campaign-form").hidden = false;
    };
    $('[data-close]').onclick = () => {
      $("#campaign-form").hidden = true;
    };

    $("#campaign-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      await api("/api/campaigns", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(Object.fromEntries(form)),
      });
      event.target.hidden = true;
      event.target.reset();
      toast("战役已创建");
      loadCampaigns();
    };

    $("#invite-accept-form").onsubmit = async (event) => {
      event.preventDefault();
      const token = new FormData(event.target).get("token");
      try {
        const result = await api("/api/invites/accept", {
          method: "POST",
          body: JSON.stringify({ token, message: "" }),
        });
        toast(result.status === "approved" ? "已加入战役" : "加入申请已提交");
        event.target.reset();
        loadCampaigns();
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return { initialize, loadCampaigns };
}
