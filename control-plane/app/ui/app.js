import { api, session } from "./api.js";
import { h, clear, badge, toast } from "./dom.js";
import { WORDMARK_SVG, MARK_SVG } from "./brand.js";
import { t, getLang, setLang } from "./i18n.js";
import * as v from "./views.js";
import * as acc from "./account.js";
import { explorerView } from "./explorer.js";
import { approvalsView } from "./approvals.js";
import { notificationsView } from "./notifications.js";
import { siemView } from "./siem.js";
import { clustersView } from "./clusters.js";
import { serversView } from "./servers.js";
import { onboardingView } from "./onboarding.js";

const app = document.getElementById("app");
let me = null;
const can = (p) => !!me && me.permissions.includes(p);

const NAV = [
  { name: "overview", icon: "overview", group: "", label: () => t("overview"), allowed: () => true },
  { name: "projects", icon: "projects", group: "", label: () => t("projects"), allowed: () => true },
  { name: "approvals", icon: "approvals", group: "", label: () => t("approvals"), allowed: () => true },
  { name: "explorer", icon: "explorer", group: "", label: () => t("explorer"), allowed: () => can("audit:read") },
  { name: "clusters", icon: "clusters", group: "infra", label: () => t("clusters"), allowed: () => can("integrations:manage") && (me.features || []).includes("multi_cluster") },
  { name: "servers", icon: "servers", group: "infra", label: () => t("servers"), allowed: () => can("integrations:manage") && (me.features || []).includes("servers") },
  { name: "notifications", icon: "connections", group: "infra", label: () => t("notifications"), allowed: () => can("notifications:manage") },
  { name: "siem", icon: "audit", group: "infra", label: () => "SIEM", allowed: () => can("integrations:manage") && (me.features || []).includes("siem") },
  { name: "audit", icon: "audit", group: "security", label: () => t("audit"), allowed: () => can("audit:read") },
  { name: "users", icon: "users", group: "security", label: () => t("users"), allowed: () => can("users:manage") },
  { name: "tokens", icon: "secrets", group: "security", label: () => t("tokens"), allowed: () => can("tokens:manage") },
  { name: "license", icon: "plans", group: "account", label: () => t("licenseAndPlans"), allowed: () => can("license:read") },
];
const GROUPS = { "": null, infra: "navInfra", security: "navSecurity", account: "navAccount" };
const ICONS = {
  overview: '<path d="M3 3h7v8H3zM14 3h7v5h-7zM14 11h7v10h-7zM3 14h7v7H3z"/>', projects: '<path d="M3 6a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
  approvals: '<path d="m4 12.5 5 5L20 6.5"/>', explorer: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  clusters: '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M12 7v5M12 12l-6 5M12 12l6 5"/>', servers: '<rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><path d="M7 7h.01M7 17h.01"/>',
  connections: '<path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6M10 19a2 2 0 0 0 4 0"/>', audit: '<path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6"/>', users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0M16 5a3.2 3.2 0 0 1 0 6M18 20a5 5 0 0 0-2.5-4.3"/>',
  secrets: '<rect x="5" y="10" width="14" height="10" rx="1.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>', plans: '<path d="M4 7h16M4 12h16M4 17h10"/>',
};
const icon = (n, raw, cls = "i") => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("class", cls === "i" ? "i" : `i ${cls}`); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.innerHTML = raw || ICONS[n] || ""; return s; };

/* тема: светлая или тёмная; при первом запуске берётся тема устройства (theme-init.js), дальше выбор пользователя */
const getTheme = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
function setTheme(v) { document.documentElement.dataset.theme = v; try { localStorage.setItem("dsp_theme", v); } catch { /* режим без хранилища */ } }

async function loadMe() {
  me = await api("/api/me");
  if (me.kind === "session") session.setCookieSession(me.csrf_token);   // после перезагрузки страницы CSRF-токен берём с сервера
  return me;
}

async function logout() {
  try { if (me && me.kind === "session") await api("/api/auth/logout", { method: "POST", redirectOn401: false }); } catch { /* сессия уже недействительна */ }
  session.clear(); me = null; location.hash = "#/login";
}

const THEME_ICONS = {
  light: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
  dark: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
};

function topbar(active, minimal = false) {
  const cur = getTheme();
  const themeNow = () => getTheme();
  const themeToggle = h("button", { type: "button", class: "btn small theme-toggle", "data-testid": "theme-toggle", "aria-label": t("themeLbl"), title: t("themeLbl"),
    onclick: () => { setTheme(themeNow() === "dark" ? "light" : "dark"); paint(); } });
  const paint = () => { themeToggle.replaceChildren(icon(null, THEME_ICONS[themeNow()])); themeToggle.dataset.theme = themeNow(); };
  paint();
  const FLAGS = { en: ["gb", "English"], ru: ["ru", "Русский"], kk: ["kz", "Қазақша"] };
  const GLOBE = '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 3.9 5.7 3.9 9S14.6 18.3 12 21c-2.6-2.7-3.9-5.7-3.9-9S9.4 5.7 12 3z"/>';
  const langMenu = h("div", { class: "lang-menu" });
  const langBtn = h("button", { type: "button", class: "btn small", "data-testid": "lang-menu", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": "language", title: "language" });
  langBtn.append(icon(null, GLOBE), h("span", { class: "lang-cur" }, getLang().toUpperCase()));
  const items = h("div", { class: "menu", role: "menu", hidden: true }, ["en", "ru", "kk"].map((l) => { const [f, name] = FLAGS[l];
    const it = h("button", { type: "button", role: "menuitem", class: getLang() === l ? "on" : "", "data-testid": `lang-${l}`, onclick: () => { if (getLang() !== l) { setLang(l); render(); } else items.hidden = true; } });
    it.append(h("img", { class: "flag", src: `flags/${f}.svg`, width: "20", height: "15", alt: "" }), h("span", {}, name)); return it; }));
  langBtn.onclick = (ev) => { ev.stopPropagation(); items.hidden = !items.hidden; langBtn.setAttribute("aria-expanded", String(!items.hidden)); };
  document.addEventListener("click", () => { items.hidden = true; langBtn.setAttribute("aria-expanded", "false"); }, { once: false });
  langMenu.append(langBtn, items);
  const brand = h("a", { class: "brand", href: "#/overview", "aria-label": "SplitWave" }); brand.innerHTML = WORDMARK_SVG + MARK_SVG;
  const out = h("button", { class: "btn small", type: "button", "data-testid": "logout", "aria-label": t("logout"), onclick: logout }, h("span", { class: "lbl" }, t("logout")));
  out.prepend(icon(null, '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M16 8l4 4-4 4M20 12H9"/>', "out-ic"));
  return h("header", { class: "topbar" }, h("div", { class: "topbar-in" },
    brand,
    h("div", { class: "who" },
      langMenu, themeToggle,
      minimal ? h("span", { class: "name", "data-testid": "who" }, me.name) : h("a", { class: "name", href: "#/account", "data-testid": "who", title: t("account") }, me.name),
      badge(me.role, me.role === "admin" ? "pro" : ""),
      out)));
}

function sidebar(active) {
  const items = NAV.filter((n) => n.allowed());
  const nodes = []; let last = null;
  for (const n of items) {
    if (n.group !== last && GROUPS[n.group]) nodes.push(h("div", { class: "grp" }, t(GROUPS[n.group])));
    last = n.group;
    nodes.push(h("a", { href: `#/${n.name}`, class: n.name === active ? "active" : "", "data-testid": `nav-${n.name}` }, icon(n.icon), h("span", {}, n.label())));
  }
  return h("aside", { class: "side" }, h("nav", { class: "nav", "aria-label": "main" }, nodes));
}

let renderId = 0;   // номер отрисовки: устаревшие (быстрая смена страниц) сами прекращаются

async function render() {
  const id = ++renderId;
  const [name = "overview", a, b] = (location.hash.replace(/^#\/?/, "") || "overview").split("/").map(decodeURIComponent);
  document.title = t("app");
  if (name === "sso-done") {                            // провайдер вернул пользователя: сессионная cookie уже выдана сервером
    session.setCookieSession(null);
    try { await loadMe(); } catch { return; }
    location.hash = "#/overview";
    return;
  }
  if (name === "login") {
    me = null;
    clear(app);
    return acc.loginView(app, async () => { await loadMe(); location.hash = "#/overview"; });
  }
  if (!me && !session.mode && !session.hint) { location.hash = "#/login"; return; }   // входа ещё не было — сразу форма входа
  try { await loadMe(); } catch { return; }            // права берём заново при каждом переходе: изменения ролей и членства применяются сразу; 401 уже перенаправил на вход
  if (id !== renderId) return;                           // за время запроса открыли другую страницу
  clear(app);
  const main = h("main", { class: "page" });
  const shell = h("div", { class: "shell" });
  app.append(shell);
  if (me.restricted) {                                   // обязательный шаг: смена пароля или настройка 2FA
    shell.append(topbar("", true), main);
    shell.style.gridTemplateColumns = "minmax(0, 1fr)"; main.style.gridColumn = "1";
    return acc.restrictedView(main, me, async () => { await loadMe(); render(); });
  }
  const active = name === "project" || name === "new" ? "projects" : name;
  shell.append(topbar(active), sidebar(active), main);
  if (can("tokens:manage")) {                            // администратору — баннер, если вышла новая версия (проверку можно выключить UPDATE_CHECK=false)
    api("/api/updates").then((u) => {
      if (!u.available || id !== renderId) return;
      main.prepend(h("div", { class: "callout", role: "note", "data-testid": "update-banner" },
        u.security ? h("strong", {}, t("updateSecurity") + " ") : null, t("updateAvailable", { v: u.latest, c: u.current }), " ",
        u.url && /^https:\/\//.test(u.url) ? h("a", { href: u.url, target: "_blank", rel: "noopener noreferrer" }, t("updateDetails")) : null));
    }).catch(() => {});
  }
  if (!me.permissions.length && !me.memberships.length && !me.project_scope && name !== "account") {
    main.append(h("div", { class: "card" }, h("div", { class: "card-body", "data-testid": "no-access-yet" }, h("h2", {}, t("noAccessYet")), h("p", { class: "muted" }, t("noAccessYetHint")))));
    return;
  }
  api("/api/approvals?status=pending&limit=100").then((rows) => {
    const link = app.querySelector('[data-testid="nav-approvals"]');
    if (link && rows.length && id === renderId) link.append(badge(String(rows.length), "warn"));
  }).catch(() => {});
  const ctx = { me, can, reloadMe: async () => { await loadMe(); render(); } };
  const guard = (perm) => { if (perm && !can(perm)) { main.append(h("div", { class: "empty" }, t("noAccess"))); return false; } return true; };
  try {
    if (name === "overview") await v.overviewView(main, ctx);
    else if (name === "project") await v.projectView(main, ctx, a, b);
    else if (name === "projects") await v.projectsView(main, ctx);
    else if (name === "new") { if (guard("projects:manage")) await onboardingView(main, ctx); }
    else if (name === "approvals") await approvalsView(main, ctx);
    else if (name === "account") await acc.accountView(main, ctx);
    else if (name === "users") { if (guard("users:manage")) await acc.usersView(main, ctx); }
    else if (name === "explorer") { if (guard("audit:read")) await explorerView(main); }
    else if (name === "audit") { if (guard("audit:read")) await v.auditView(main, ctx); }
    else if (name === "notifications") { if (guard("notifications:manage")) await notificationsView(main, ctx); }
    else if (name === "siem") { if (guard("integrations:manage")) await siemView(main, ctx); }
    else if (name === "clusters") { if (guard("integrations:manage")) await clustersView(main, ctx); }
    else if (name === "servers") { if (guard("integrations:manage")) await serversView(main, ctx); }
    else if (name === "tokens") { if (guard("tokens:manage")) await v.tokensView(main, ctx); }
    else if (name === "license") { if (guard("license:read")) await v.licenseView(main, ctx); }
    else await v.overviewView(main, ctx);
  } catch (e) { toast(e.message || String(e), "bad"); }
}

window.addEventListener("hashchange", render);
setLang(getLang());
render();
