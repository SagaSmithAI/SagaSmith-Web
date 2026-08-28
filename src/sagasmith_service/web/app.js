import { api } from "/assets/api/client.js";
import { createAuthController } from "/assets/auth/controller.js";
import { createCampaignController } from "/assets/campaign/controller.js";
import { $, $$, text } from "/assets/components/dom.js";
import { registerServiceWorker } from "/assets/components/pwa.js";
import { toast } from "/assets/components/toast.js";
import { createForgeController } from "/assets/forge/controller.js";
import { createIdentityController } from "/assets/identity/controller.js";
import { createModuleStudioController } from "/assets/module-studio/controller.js";
import { createRoomController } from "/assets/room/controller.js";
import { state } from "/assets/state/store.js";

let roomController;
const campaignController = createCampaignController({
  openCampaign: (campaign) => roomController.openCampaign(campaign),
});
const moduleStudioController = createModuleStudioController();
const identityController = createIdentityController();
const forgeController = createForgeController({
  loadPacks,
  loadSoulOptions: identityController.loadSoulOptions,
});
roomController = createRoomController({
  loadCampaigns: campaignController.loadCampaigns,
  loadUsage,
  loadCampaignIdentities: identityController.loadCampaignIdentities,
  loadIdentityInviteOptions: identityController.loadIdentityInviteOptions,
  openModuleStudio: () => {
    const nav = $('.nav[data-view="modules"]');
    if (nav) nav.click();
  },
});
const authController = createAuthController({
  onAuthenticated: () => {
    campaignController.loadCampaigns();
    loadUsage();
  },
});

$$('.nav[data-view]').forEach((button) => {
  button.onclick = () => {
    $$(".nav").forEach((item) => item.classList.toggle("active", item === button));
    $$(".view").forEach((view) => {
      view.hidden = view.id !== `${button.dataset.view}-view`;
    });
    if (button.dataset.view === "packs") loadPacks();
    if (button.dataset.view === "usage") loadUsage();
    if (button.dataset.view === "forge") forgeController.loadForge();
    if (button.dataset.view === "studio") forgeController.loadStudio();
    if (button.dataset.view === "identities") identityController.loadIdentities();
    if (button.dataset.view === "modules") moduleStudioController.loadModules();
  };
});

async function loadUsage() {
  if (!state.user) return;
  const [balance, ledger] = await Promise.all([
    api("/api/usage/balance"),
    api("/api/usage/ledger"),
  ]);
  const total = Number(balance.granted);
  const used = Number(balance.used);
  const percentage = total ? Math.min(100, (used / total) * 100) : 0;
  const root = $("#usage-card");
  root.replaceChildren(
    text("p", `${used.toLocaleString()} / ${total.toLocaleString()} tokens 已使用`, "muted"),
  );
  const meter = text("div", "", "meter");
  const fill = text("span", "");
  fill.style.width = `${percentage}%`;
  meter.append(fill);
  root.append(
    meter,
    text("p", `可用 ${Number(balance.available).toLocaleString()} tokens`, "eyebrow"),
  );
  $("#usage-ledger").replaceChildren(
    text("h3", "最近用量"),
    ...ledger.map((item) =>
      text(
        "p",
        `${new Date(item.occurred_at).toLocaleString()} · ${item.quantity} ${item.unit} · ${item.model || item.provider || "—"}`,
        "muted",
      ),
    ),
  );
}

$("#pack-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    await api("/api/packs", { method: "POST", body: data });
    event.target.reset();
    toast("Pack 已保存到私有库");
    loadPacks();
  } catch (error) {
    toast(error.message);
  }
};

async function loadPacks() {
  state.packs = await api("/api/packs");
  const root = $("#pack-list");
  root.replaceChildren();
  for (const pack of state.packs) {
    const card = text("article", "", "card pack");
    card.append(
      text("p", pack.kind, "eyebrow"),
      text("h3", pack.title),
      text("p", `${pack.pack_id} · ${pack.version}`, "muted"),
      text("p", `${(pack.size_bytes / 1024).toFixed(1)} KB · private`, "meta"),
    );
    root.append(card);
  }
}

roomController.initialize();
campaignController.initialize();
moduleStudioController.initialize();
identityController.initialize();
forgeController.initialize();
authController.initialize();
registerServiceWorker();
setInterval(() => {
  if (state.campaign && !$("#campaign-room").hidden) {
    roomController.refreshRuntime().catch(() => {});
  }
}, 60000);
authController.boot();
