import { api } from "/assets/api/client.js";
import { $, text } from "/assets/components/dom.js";
import { toast } from "/assets/components/toast.js";
import { state } from "/assets/state/store.js";

function formatSession(session) {
  const lastSeen = new Date(session.last_seen_at).toLocaleString();
  const expires = new Date(session.expires_at).toLocaleString();
  return `${session.current ? "当前会话" : "其他会话"} · 最近活动 ${lastSeen} · ${expires} 到期`;
}

export function createAccountController() {
  async function load() {
    if (!state.user) return;
    const sessions = await api("/api/auth/sessions");
    $("#account-email").textContent = state.user.email;
    $("#account-profile-form").elements.display_name.value = state.user.display_name;
    $("#account-sessions").replaceChildren(
      ...sessions.map((session) =>
        text("p", formatSession(session), session.current ? "session-current" : "muted"),
      ),
    );
    $("#revoke-other-sessions").disabled = sessions.every((session) => session.current);
  }

  function initialize() {
    $("#account-profile-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      try {
        state.user = (
          await api("/api/auth/me", {
            method: "PATCH",
            body: JSON.stringify({ display_name: form.get("display_name") }),
          })
        ).user;
        $("#identity").textContent = state.user.display_name;
        toast("显示名称已更新");
      } catch (error) {
        toast(error.message);
      }
    };

    $("#account-password-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      if (form.get("new_password") !== form.get("confirm_password")) {
        toast("两次输入的新密码不一致");
        return;
      }
      try {
        await api("/api/auth/password", {
          method: "POST",
          body: JSON.stringify({
            current_password: form.get("current_password"),
            new_password: form.get("new_password"),
          }),
        });
        event.target.reset();
        toast("密码已更新，其他会话已退出");
        await load();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#revoke-other-sessions").onclick = async () => {
      try {
        await api("/api/auth/sessions/revoke-others", { method: "POST" });
        toast("其他会话已退出");
        await load();
      } catch (error) {
        toast(error.message);
      }
    };

    $("#account-deactivate-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      if (form.get("confirmation") !== "DEACTIVATE") {
        toast("请输入 DEACTIVATE 确认停用");
        return;
      }
      try {
        await api("/api/auth/deactivate", {
          method: "POST",
          body: JSON.stringify({
            current_password: form.get("current_password"),
            confirmation: form.get("confirmation"),
          }),
        });
        location.reload();
      } catch (error) {
        toast(error.message);
      }
    };
  }

  return { initialize, load };
}
