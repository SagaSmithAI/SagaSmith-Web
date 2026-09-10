import { api } from "/assets/api/client.js";
import { $, showLoadState, text, withBusy } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { state } from "/assets/state/store.js";

export function createCampaignController({ openCampaign }) {
  let loading;
  async function loadCampaigns({ fresh = false } = {}) {
    if (loading) {
      if (!fresh) return loading;
      await loading;
    }
    const list = $("#campaign-list");
    list.setAttribute("aria-busy", "true");
    if (!state.campaigns.length) showLoadState(list, "正在加载战役…");
    loading = (async () => {
      try {
        state.campaigns = await api("/api/campaigns");
        const fragment = document.createDocumentFragment();
        for (const campaign of state.campaigns) {
          const card = text("article", "", "card campaign");
          card.setAttribute("role", "button");
          card.tabIndex = 0;
          card.setAttribute("aria-label", `打开战役：${campaign.name}`);
          card.append(
            text("p", campaign.system_id.toUpperCase(), "eyebrow"),
            text("h3", campaign.name),
          );
          const meta = text("div", "", "meta");
          meta.append(text("span", campaign.visibility), text("span", `rev ${campaign.mcp_revision}`));
          card.append(meta);
          card.onclick = () => openCampaign(campaign);
          card.onkeydown = (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              card.click();
            }
          };
          fragment.append(card);
        }
        list.replaceChildren(fragment);
        if (!state.campaigns.length) showLoadState(list, "还没有战役。创建一个战役，或使用邀请代码加入。");
      } catch (error) {
        showLoadState(list, `战役加载失败：${error.message}`, loadCampaigns);
      } finally {
        list.removeAttribute("aria-busy");
        loading = null;
      }
    })();
    return loading;
  }

  function initialize() {
    $("#new-campaign").onclick = () => {
      $("#campaign-form").hidden = false;
      $("#campaign-form input").focus();
    };
    $('[data-close]').onclick = () => {
      $("#campaign-form").hidden = true;
      $("#new-campaign").focus();
    };

    $("#campaign-form").onsubmit = async (event) => {
      event.preventDefault();
      await withBusy(event.target, async () => {
        const form = new FormData(event.target);
        try {
          await api("/api/campaigns", {
            method: "POST",
            headers: { "Idempotency-Key": crypto.randomUUID() },
            body: JSON.stringify(Object.fromEntries(form)),
          });
          event.target.hidden = true;
          event.target.reset();
          toast("战役已创建");
          await loadCampaigns({ fresh: true });
        } catch (error) {
          toast(`创建失败：${error.message}`);
        }
      });
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
        await loadCampaigns({ fresh: true });
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return { initialize, loadCampaigns };
}
