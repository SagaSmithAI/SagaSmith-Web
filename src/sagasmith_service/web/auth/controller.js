import { api } from "/assets/api/client.js";
import { $, $$ } from "/assets/components/dom.js";
import { state } from "/assets/state/store.js";

export function createAuthController({ onAuthenticated }) {
  function showApp() {
    $("#auth").hidden = true;
    $("#app").hidden = false;
    $("#identity").textContent = state.user.display_name;
    onAuthenticated();
    if (state.user.is_admin) $("#moderation").hidden = false;
  }

  async function boot() {
    try {
      state.user = (await api("/api/auth/me")).user;
      showApp();
    } catch {
      $("#auth").hidden = false;
    }
  }

  function initialize() {
    $$('[data-mode]').forEach((control) => {
      control.onclick = () => {
        $$('[data-mode]').forEach((item) => item.classList.toggle("active", item === control));
        $("#name-row").hidden = control.dataset.mode !== "register";
        $("#terms-row").hidden = control.dataset.mode !== "register";
        $("#terms-row input").required = control.dataset.mode === "register";
        $("#auth-form input[name=password]").autocomplete =
          control.dataset.mode === "register" ? "new-password" : "current-password";
        $("#auth-form").dataset.mode = control.dataset.mode;
      };
    });

    $("#auth-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const mode = event.target.dataset.mode || "login";
      try {
        const body = { email: form.get("email"), password: form.get("password") };
        if (mode === "register") {
          body.display_name = form.get("display_name");
          body.terms_accepted = form.get("terms_accepted") === "on";
          body.terms_version = "2026-08-29";
          body.privacy_version = "2026-08-29";
        }
        state.user = (
          await api(`/api/auth/${mode}`, { method: "POST", body: JSON.stringify(body) })
        ).user;
        showApp();
      } catch (error) {
        $("#auth-error").textContent = error.message;
      }
    };

    $("#logout").onclick = async () => {
      await api("/api/auth/logout", { method: "POST" });
      location.reload();
    };
  }

  return { boot, initialize };
}
