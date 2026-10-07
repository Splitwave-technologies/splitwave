// Вход, принудительные шаги (смена пароля, 2FA), страница аккаунта, пользователи и участники проекта.
import { api, session } from "./api.js";
import { h, toast, badge, fmtTime, confirmDialog, showSecretOnce, formDialog } from "./dom.js";
import { t } from "./i18n.js";

const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const field = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);
const enc = encodeURIComponent;
const refresh = () => window.dispatchEvent(new Event("hashchange"));

/* ───────────── вход ───────────── */
export function loginView(root, onSuccess) {
  let mode = "password";      // "password" | "token"
  const box = h("div", { class: "card-body" });
  const wrap = h("div", { class: "login" }, h("div", { class: "login-card card" },
    h("div", { class: "card-head" }, h("span", { class: "card-title" }, `${t("app")} · ${t("login")}`)), box));
  root.append(wrap);

  function draw(needTotp = false, keep = {}) {
    const err = h("div", { class: "error-text", "data-testid": "login-error", role: "alert" });
    if (mode === "token") {
      const input = h("input", { type: "password", id: "token", autocomplete: "off", "aria-label": t("token"), "data-testid": "token-input" });
      box.replaceChildren(h("form", { class: "card-body", onsubmit: async (e) => {
        e.preventDefault(); err.textContent = ""; session.setToken(input.value.trim());
        try { await api("/api/me", { redirectOn401: false }); await onSuccess(); } catch { session.clear(); err.textContent = t("badToken"); }
      } }, h("p", { class: "muted" }, t("loginHint")), field(t("token"), input), err,
      h("button", { class: "btn primary", type: "submit", "data-testid": "token-submit" }, t("signIn")),
      h("button", { class: "btn small", type: "button", "data-testid": "use-account", onclick: () => { mode = "password"; draw(); } }, t("useAccount"))));
      input.focus();
      return;
    }
    const user = h("input", { id: "username", autocomplete: "username", "aria-label": t("username"), "data-testid": "login-username", value: keep.user || "" });
    const pass = h("input", { type: "password", id: "password", autocomplete: "current-password", "aria-label": t("password"), "data-testid": "login-password", value: keep.pass || "" });
    const code = h("input", { id: "totp", autocomplete: "one-time-code", inputmode: "numeric", "aria-label": t("totpCode"), "data-testid": "login-totp" });
    box.replaceChildren(h("form", { class: "card-body", onsubmit: async (e) => {
      e.preventDefault(); err.textContent = "";
      try {
        const r = await api("/api/auth/login", { method: "POST", redirectOn401: false,
          body: { username: user.value.trim(), password: pass.value, totp: needTotp ? code.value.trim() : undefined } });
        session.clear(); session.setCookieSession(r.csrf_token);
        await onSuccess();
      } catch (ex) {
        if (ex.detail && ex.detail.error === "totp_required") { draw(true, { user: user.value, pass: pass.value }); return; }
        err.textContent = ex.status === 429 ? t("tooManyAttempts") : t("badCredentials");
      }
    } }, field(t("username"), user), field(t("password"), pass), needTotp ? field(t("totpCode"), code) : null,
    needTotp ? h("p", { class: "small muted" }, t("totpHint")) : null, err,
    h("button", { class: "btn primary", type: "submit", "data-testid": "login-submit" }, t("signIn")),
    h("button", { class: "btn small", type: "button", "data-testid": "use-token", onclick: () => { mode = "token"; draw(); } }, t("useToken"))));
    (needTotp ? code : user).focus();
    if (!needTotp) offerSso();
  }
  async function offerSso() {          // кнопки входа через провайдера (их подключает платный модуль SSO)
    try {
      const list = await api("/api/auth/providers", { redirectOn401: false });
      if (!list.length || mode !== "password" || box.querySelector('[data-testid="sso-login"]')) return;
      box.append(h("div", { class: "card-body" }, list.map((p) => h("a", { class: "btn", href: p.url, "data-testid": "sso-login" }, t("ssoSignIn", { n: p.name })))));
    } catch { /* список недоступен — остаётся вход по паролю */ }
  }
  const ssoErr = (location.hash.split("/")[2] || "");
  draw();
  if (ssoErr) { const e = box.querySelector('[data-testid="login-error"]'); if (e) e.textContent = t("ssoError", { c: ssoErr }); }
}

/* ───────────── принудительные шаги ───────────── */
function passwordForm(onDone, { requireCurrent = true } = {}) {
  const cur = h("input", { type: "password", autocomplete: "current-password", "aria-label": t("currentPassword"), "data-testid": "pw-current" });
  const nw = h("input", { type: "password", autocomplete: "new-password", "aria-label": t("newPassword"), "data-testid": "pw-new" });
  return h("div", { class: "form-grid" }, requireCurrent ? field(t("currentPassword"), cur) : null, field(t("newPassword"), nw),
    h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "pw-save", onclick: async () => {
      try { await api("/api/auth/password", { method: "POST", body: { current: cur.value, new: nw.value } }); toast(t("passwordChanged"), "ok"); onDone(); }
      catch (e) { toast(e.message, "bad"); }
    } }, t("changePassword"))), h("p", { class: "small muted" }, t("passwordRules")));
}

function totpSetup(onDone) {
  const host = h("div", { class: "card-body" }, h("div", { class: "muted" }, t("loading")));
  const start = async () => {
    try {
      const r = await api("/api/auth/totp/setup", { method: "POST" });
      const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", "aria-label": t("totpCode"), "data-testid": "totp-code" });
      host.replaceChildren(h("p", { class: "muted" }, t("totpSetupHint")),
        h("div", { class: "kv" }, h("dt", {}, t("totpSecret")), h("dd", { class: "mono", "data-testid": "totp-secret" }, r.secret),
          h("dt", {}, "URI"), h("dd", { class: "mono small ellipsis", title: r.uri }, r.uri)),
        field(t("totpCode"), code),
        h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "totp-enable", onclick: async () => {
          try {
            const res = await api("/api/auth/totp/enable", { method: "POST", body: { code: code.value.trim() } });
            showSecretOnce({ title: t("recoveryCodes"), note: t("recoveryNote"), value: res.recovery_codes.join("\n") });
            toast(t("totpEnabled"), "ok"); onDone();
          } catch (e) { toast(e.message, "bad"); }
        } }, t("enable"))));
    } catch (e) { host.replaceChildren(h("div", { class: "empty" }, e.message)); }
  };
  start();
  return host;
}

export function restrictedView(root, me, onDone) {
  const isPw = me.restricted === "password_change";
  root.append(h("div", { class: "page-head" }, h("h1", {}, isPw ? t("mustChangePassword") : t("mustEnable2fa"))),
    card(isPw ? t("changePassword") : t("twoFactor"), isPw ? h("div", { class: "card-body" }, passwordForm(onDone)) : totpSetup(onDone)));
}

/* ───────────── аккаунт ───────────── */
export async function accountView(root, ctx) {
  const me = ctx.me;
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("account"))));
  root.append(card(t("profile"), h("div", { class: "card-body" }, h("dl", { class: "kv" },
    h("dt", {}, t("username")), h("dd", { "data-testid": "acc-name" }, me.name),
    h("dt", {}, t("role")), h("dd", {}, badge(me.role, me.role === "admin" ? "pro" : "")),
    h("dt", {}, t("signInMethod")), h("dd", {}, me.kind === "session" ? t("viaAccount") : t("viaToken")),
    h("dt", {}, t("projects")), h("dd", {}, me.project_scope ? me.project_scope : me.memberships.length ? me.memberships.map((m) => `${m.project} (${m.role})`).join(", ") : "—")))));
  if (me.kind !== "session") return;

  root.append(card(t("changePassword"), h("div", { class: "card-body" }, passwordForm(() => refresh()))));

  const twoBody = h("div", { class: "card-body" });
  if (me.totp_enabled) {
    const pw = h("input", { type: "password", autocomplete: "current-password", "aria-label": t("password"), "data-testid": "totp-off-password" });
    const code = h("input", { inputmode: "numeric", "aria-label": t("totpCode"), "data-testid": "totp-off-code" });
    twoBody.append(h("p", {}, badge(t("enabledLbl"), "ok")), h("p", { class: "small muted" }, t("totpDisableHint")),
      h("div", { class: "form-grid" }, field(t("password"), pw), field(t("totpCode"), code),
        h("div", { class: "row" }, h("button", { class: "btn danger", type: "button", "data-testid": "totp-disable", onclick: async () => {
          try { await api("/api/auth/totp/disable", { method: "POST", body: { password: pw.value, code: code.value.trim() } }); toast(t("totpDisabled"), "ok"); ctx.reloadMe(); }
          catch (e) { toast(e.message, "bad"); }
        } }, t("disable")))));
  } else {
    twoBody.append(h("p", {}, badge(t("disabledLbl"), "warn")), totpSetupTrigger(() => ctx.reloadMe()));
  }
  root.append(card(t("twoFactor"), twoBody));

  const sessBody = h("div", {}, h("div", { class: "empty" }, t("loading")));
  root.append(card(t("sessions"), sessBody));
  const loadSessions = async () => {
    try {
      const list = await api("/api/auth/sessions");
      sessBody.replaceChildren(tableWrap(th(t("created"), t("lastUsed"), "IP", t("device"), ""), h("tbody", {}, list.map((s) => h("tr", { "data-testid": s.current ? "session-current" : "session-other" },
        h("td", { class: "time" }, fmtTime(s.created_at)), h("td", { class: "time" }, fmtTime(s.last_seen_at)), h("td", { class: "mono" }, s.ip || "—"),
        h("td", { class: "mono ellipsis", title: s.user_agent }, (s.user_agent || "").slice(0, 40) || "—"),
        h("td", { class: "actions" }, s.current ? badge(t("thisSession"), "ok") : h("button", { class: "btn small danger", type: "button", onclick: async () => {
          try { await api(`/api/auth/sessions/${enc(s.id)}`, { method: "DELETE" }); toast(t("sessionRevoked"), "ok"); loadSessions(); } catch (e) { toast(e.message, "bad"); }
        } }, t("revoke")))))))); } catch (e) { sessBody.replaceChildren(h("div", { class: "empty" }, e.message)); }
  };
  loadSessions();
}

function totpSetupTrigger(onDone) {
  const host = h("div", {});
  const btn = h("button", { class: "btn primary", type: "button", "data-testid": "totp-start", onclick: () => { host.replaceChildren(totpSetup(onDone)); } }, t("setup2fa"));
  host.append(btn);
  return host;
}

/* ───────────── пользователи ───────────── */
const ROLE_OPTIONS = ["none", "viewer", "developer", "devops", "admin"];
export async function usersView(root, ctx) {
  const body = h("div", {}, h("div", { class: "empty" }, t("loading")));
  const teams = !!(ctx.me && ctx.me.teams);                                                  // без платного модуля команды — одна встроенная учётная запись
  const newBtn = h("button", { class: "btn primary", type: "button", "data-testid": "user-new", onclick: newUserDialog }, t("newUser"));
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("users")), newBtn),
    teams ? null : h("div", { class: "callout", "data-testid": "teams-paid" }, t("teamsPaid")), card(t("users"), body));

  function newUserDialog() {
    const name = h("input", { "aria-label": t("username"), placeholder: "alice", autocomplete: "off", "data-testid": "nu-name" });
    const role = h("select", { "aria-label": t("role"), value: "none", "data-testid": "nu-role" }, ROLE_OPTIONS.map((r) => h("option", { value: r }, `${r} — ${t("roleDesc_" + r)}`)));
    const pw = h("input", { type: "password", "aria-label": t("password"), autocomplete: "new-password", placeholder: t("optionalPassword"), "data-testid": "nu-pass" });
    formDialog(t("newUser"), h("div", { class: "form-grid" }, field(t("username"), name), field(t("role"), role), field(t("password"), pw)),
      (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "nu-create", onclick: async () => {
        try {
          const r = await api("/api/users", { method: "POST", body: { username: name.value.trim(), role: role.value, password: pw.value || undefined } });
          close();
          if (r.temporary_password) showSecretOnce({ title: t("userCreated"), note: t("tempPasswordNote"), value: r.temporary_password });
          else toast(t("userCreated"), "ok");
          load();
        } catch (e) { toast(e.message, "bad"); }
      } }, t("create"))]);
  }

  async function act(user, patch, okKey) {
    try {
      const r = await api(`/api/users/${enc(user.id)}`, { method: "PATCH", body: patch });
      if (r.temporary_password) showSecretOnce({ title: t("passwordReset"), note: t("tempPasswordNote"), value: r.temporary_password });
      else toast(t(okKey), "ok");
      load();
    } catch (e) { toast(e.message, "bad"); load(); }
  }

  async function load() {
    try {
      const list = await api("/api/users");
      newBtn.hidden = !teams && list.length >= 1;
      if (!list.length) { body.replaceChildren(h("div", { class: "empty" }, t("noUsers"))); return; }
      body.replaceChildren(tableWrap(th(t("username"), t("role"), t("status"), "2FA", t("projects"), t("lastLogin"), ""), h("tbody", {}, list.map((u) => {
        const roleSel = !teams ? h("span", {}, u.role) : h("select", { "aria-label": t("role"), value: u.role, "data-testid": `role-${u.username}`, onchange: (e) => act(u, { role: e.target.value }, "userUpdated") },
          ROLE_OPTIONS.map((r) => h("option", { value: r }, r)));
        return h("tr", { "data-testid": `user-${u.username}` },
          h("td", { class: "mono" }, u.username), h("td", {}, roleSel),
          h("td", {}, u.disabled ? badge(t("disabledLbl"), "bad") : u.must_change_password ? badge(t("pendingPw"), "warn") : badge(t("active"), "ok")),
          h("td", {}, u.totp_enabled ? badge(t("enabledLbl"), "ok") : badge(t("disabledLbl"))),
          h("td", { class: "small muted" }, u.memberships.length ? u.memberships.map((m) => `${m.project} (${m.role})`).join(", ") : "—"),
          h("td", { class: "time" }, fmtTime(u.last_login_at)),
          h("td", { class: "actions wrap" },
            teams ? [h("button", { class: "btn small", type: "button", "data-testid": `toggle-${u.username}`, onclick: () => act(u, { disabled: !u.disabled }, "userUpdated") }, u.disabled ? t("enableUser") : t("disableUser")), " "] : null,
            h("button", { class: "btn small", type: "button", "data-testid": `resetpw-${u.username}`, onclick: () => act(u, { reset_password: true }, "userUpdated") }, t("resetPassword")), " ",
            u.totp_enabled ? [h("button", { class: "btn small", type: "button", onclick: () => act(u, { reset_totp: true }, "userUpdated") }, t("reset2fa")), " "] : null,
            teams ? h("button", { class: "btn small danger", type: "button", "data-testid": `deluser-${u.username}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("confirmDeleteUser", { n: u.username }), danger: true, confirmLabel: t("delete"), expect: u.username }))) return;
              try { await api(`/api/users/${enc(u.id)}`, { method: "DELETE" }); toast(t("userDeleted"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete")) : null));
      }))));
    } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
  }
  load();
}

/* ───────────── участники проекта (карточка на странице проекта) ───────────── */
export function membersCard(slug, ctx) {
  if (!(ctx.me && ctx.me.teams)) return card(t("members"), h("div", { class: "empty", "data-testid": "teams-paid" }, t("teamsPaid")));
  const body = h("div", {}, h("div", { class: "empty" }, t("loading")));
  const canManage = ctx.can("users:manage");
  const base = `/api/projects/${enc(slug)}/members`;
  const nameIn = h("input", { "aria-label": t("username"), placeholder: "alice", autocomplete: "off", "data-testid": "mem-name" });
  const roleSel = h("select", { "aria-label": t("role"), value: "developer", "data-testid": "mem-role" }, ["viewer", "developer", "devops"].map((r) => h("option", { value: r }, r)));
  async function load() {
    try {
      const list = await api(base);
      body.replaceChildren(list.length ? tableWrap(th(t("username"), t("role"), ""), h("tbody", {}, list.map((m) => h("tr", { "data-testid": `member-${m.username}` },
        h("td", { class: "mono" }, m.username), h("td", {}, badge(m.role)),
        h("td", { class: "actions" }, canManage ? h("button", { class: "btn small danger", type: "button", onclick: async () => {
          try { await api(`${base}/${enc(m.username)}`, { method: "DELETE" }); toast(t("memberRemoved"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
        } }, t("delete")) : null))))) : h("div", { class: "empty" }, t("noMembers")));
    } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
  }
  load();
  return card(t("members"), h("div", {}, canManage ? h("div", { class: "card-body form-grid" }, field(t("username"), nameIn), field(t("role"), roleSel),
    h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "mem-add", onclick: async () => {
      try { await api(`${base}/${enc(nameIn.value.trim())}`, { method: "PUT", body: { role: roleSel.value } }); nameIn.value = ""; toast(t("memberSaved"), "ok"); load(); }
      catch (e) { toast(e.message, "bad"); }
    } }, t("addMember")))) : null, body));
}
