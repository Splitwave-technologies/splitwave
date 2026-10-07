import { api, qs, session } from "./api.js";
import { h, clear, toast, badge, fmtTime, shortImage, confirmDialog, showSecretOnce, formDialog } from "./dom.js";
import { t } from "./i18n.js";
import { membersCard } from "./account.js";
import { envImportCard } from "./envimport.js";
import { connectionsDialog } from "./connections.js";
import { certSection } from "./domaincert.js";
import { pendingCard } from "./approvals.js";
import { columnChart, hbars } from "./charts.js";

let pollGen = 0; // номер отрисовки: устаревшие опросы релизов сами останавливаются

const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const errorBox = (e) => h("div", { class: "empty" }, `${t("error")}: ${e.message || e}`);
const loading = () => h("div", { class: "empty" }, t("loading"));
const enc = encodeURIComponent;
const projectHref = (slug, env) => `#/project/${enc(slug)}/${enc(env)}`;

function statusBadge(row) {
  if (row.state === "failed") { const b = badge(t("deployFailed"), "bad"); if (row.error) b.title = row.error; return b; }
  if (row.ready === true) return badge(t("ready"), "ok");
  if (row.ready === false) return badge(t("building"), "busy");
  const b = badge(t("unknown"), "warn");
  if (row.error) b.title = row.error;
  return b;
}
const releaseBadge = (s) => badge(s, { deployed: "ok", failed: "bad", building: "busy", pending: "warn" }[s] || "");

/* ───────────── вход ───────────── */
export function loginView(root, onSuccess) {
  const input = h("input", { type: "password", id: "token", autocomplete: "off", "aria-label": t("token"), "data-testid": "token-input" });
  const err = h("div", { class: "error-text", "data-testid": "login-error", role: "alert" });
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      session.set(input.value.trim());
      try { await api("/api/me", { redirectOn401: false }); await onSuccess(); } catch { session.clear(); err.textContent = t("badToken"); }
    },
  },
  h("div", { class: "card-body" },
    h("p", { class: "muted" }, t("loginHint")),
    h("div", { class: "field" }, h("label", { for: "token" }, t("token")), input),
    err,
    h("button", { class: "btn primary", type: "submit" }, t("signIn"))));
  root.append(h("div", { class: "login" }, h("div", { class: "login-card card" },
    h("div", { class: "card-head" }, h("span", { class: "card-title" }, `${t("app")} · ${t("login")}`)), form)));
  input.focus();
}

/* ───────────── проекты ───────────── */
function newProjectDialog() {
  const f = {
    slug: h("input", { "aria-label": t("slug"), placeholder: "my-app", "data-testid": "np-slug" }),
    repo: h("input", { "aria-label": t("repo"), placeholder: "owner/repo", "data-testid": "np-repo" }),
    provider: h("select", { "aria-label": t("provider"), "data-testid": "np-provider" },
      [["github", "GitHub"], ["gitlab", "GitLab"], ["bitbucket", "Bitbucket"], ["gitea", "Gitea / Forgejo"]].map(([v, l]) => h("option", { value: v }, l))),
    method: h("select", { "aria-label": t("buildMethod"), "data-testid": "np-method" },
      h("option", { value: "buildpacks" }, t("methodBuildpacks")), h("option", { value: "dockerfile" }, "Dockerfile")),
    gitUrl: h("input", { "aria-label": t("gitUrl"), placeholder: "https://git.example.com/owner/repo.git", "data-testid": "np-giturl" }),
    gitToken: h("input", { type: "password", "aria-label": t("gitToken"), autocomplete: "new-password", "data-testid": "np-gittoken" }),
    env: h("input", { "aria-label": t("envName"), value: "prod" }),
    ns: h("input", { "aria-label": t("namespace"), placeholder: "default", "data-testid": "np-ns" }),
    dep: h("input", { "aria-label": t("deployment"), "data-testid": "np-dep" }),
    con: h("input", { "aria-label": t("container"), "data-testid": "np-con" }),
    branch: h("input", { "aria-label": t("branch"), value: "main" }),
  };
  const field = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);
  const body = h("div", { class: "form-grid" }, field(t("slug"), f.slug), field(t("provider"), f.provider), field(t("repo"), f.repo), field(t("buildMethod"), f.method),
    field(`${t("gitUrl")} (${t("optional")})`, f.gitUrl), field(`${t("gitToken")} (${t("optional")})`, f.gitToken), field(t("envName"), f.env),
    field(t("namespace"), f.ns), field(t("deployment"), f.dep), field(t("container"), f.con), field(t("branch"), f.branch));
  formDialog(t("newProject"), body, (close) => [
    h("button", { class: "btn primary", type: "button", "data-testid": "np-create", onclick: async () => {
      try {
        await api("/api/projects", { method: "POST", body: {
          slug: f.slug.value.trim(), repo_full_name: f.repo.value.trim(), provider: f.provider.value, build_method: f.method.value,
          git_url: f.gitUrl.value.trim() || undefined, git_token: f.gitToken.value.trim() || undefined,
          environments: [{ name: f.env.value.trim(), namespace: f.ns.value.trim(), deployment_name: f.dep.value.trim(),
            container_name: f.con.value.trim(), branch: f.branch.value.trim() || "main" }] } });
        close(); toast(t("projectCreated"), "ok");
        location.hash = projectHref(f.slug.value.trim(), f.env.value.trim());
      } catch (e) { toast(e.message, "bad"); }
    } }, t("create")),
  ]);
}

export async function projectsView(root, ctx) {
  const body = loading();
  const actions = ctx.can("projects:manage") ? h("div", { class: "row" },
    h("button", { class: "btn", type: "button", "data-testid": "new-project", onclick: newProjectDialog }, t("manualProject")),
    h("a", { class: "btn primary", href: "#/new", "data-testid": "onboard" }, t("onboard"))) : null;
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("projects")), actions), card(t("projects"), body));
  try {
    const rows = await api("/api/projects");
    if (actions && !(ctx.me && ctx.me.projects) && rows.length >= 1) {                       // без платного модуля — один проект
      actions.replaceWith(h("div", { class: "callout", "data-testid": "projects-paid" }, t("projectsPaid")));
    }
    if (!rows.length) { body.replaceWith(h("div", { class: "empty" }, t("noProjects"), " ", ctx.can("projects:manage") ? h("a", { href: "#/new" }, t("onboard")) : null)); return; }
    body.replaceWith(tableWrap(
      th(t("projects"), t("repo"), t("environment"), t("namespace"), t("status"), t("image"), ""),
      h("tbody", {}, rows.map((r) => h("tr", { "data-testid": `row-${r.slug}` },
        h("td", {}, h("a", { href: projectHref(r.slug, r.environment) }, r.slug)),
        h("td", { class: "mono" }, r.repo), h("td", {}, r.environment), h("td", { class: "mono" }, r.namespace),
        h("td", {}, statusBadge(r)), h("td", { class: "mono" }, shortImage(r.latest_image)),
        h("td", { class: "actions" }, h("a", { href: projectHref(r.slug, r.environment) }, t("open"))))))));
  } catch (e) { body.replaceWith(errorBox(e)); }
}

/* ───────────── проект ───────────── */
const field2 = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);

export async function projectView(root, ctx, slug, envName) {
  const gen = ++pollGen;
  let rows;
  try { rows = (await api("/api/projects")).filter((r) => r.slug === slug); } catch (e) { root.append(errorBox(e)); return; }
  if (!rows.length) { root.append(h("div", { class: "empty" }, t("notFound"))); return; }
  const cur = rows.find((r) => r.environment === envName) || rows[0];
  const base = `/api/projects/${enc(slug)}`;
  // права в этом проекте: глобальные или по роли участника / ограничению токена
  const pcan = (perm) => ctx.can(perm) || ((ctx.me.project_permissions || {})[slug] || []).includes(perm);
  let envInfo = {};
  try { envInfo = (await api(`${base}/environments`)).find((e) => e.name === cur.environment) || {}; } catch { /* нет прав на список сред — считаем, что согласования нет */ }
  const needApproval = !!envInfo.require_approval;

  const envSel = h("select", { "aria-label": t("environment"), value: cur.environment,
    onchange: (e) => { location.hash = projectHref(slug, e.target.value); } },
  rows.map((r) => h("option", { value: r.environment }, r.environment)));

  root.append(h("div", { class: "page-head" },
    h("div", {}, h("div", { class: "crumbs" }, h("a", { href: "#/projects" }, t("projects")), ` / ${slug}`),
      h("h1", { "data-testid": "project-title" }, slug), h("div", { class: "mono muted" }, cur.repo)),
    h("div", { class: "row" }, envSel, needApproval ? badge(t("approvalRequired"), "pro") : null, statusBadge(cur))));

  root.append(card(t("deploy"), h("div", { class: "card-body" }, h("dl", { class: "kv" },
    h("dt", {}, t("namespace")), h("dd", { class: "mono" }, cur.namespace),
    h("dt", {}, t("image")), h("dd", { class: "mono" }, shortImage(cur.latest_image)),
    cur.error ? [h("dt", {}, t("error")), h("dd", { class: "mono" }, cur.error)] : null),
  h("div", { class: "row" },
    pcan("deploy") ? h("button", { class: "btn primary", type: "button", "data-testid": "redeploy", onclick: redeployDialog }, needApproval ? t("requestRedeploy") : t("redeploy")) : null,
    pcan("rollback") ? h("button", { class: "btn", type: "button", "data-testid": "rollback-last", onclick: () => rollback(null) }, needApproval ? t("requestRollback") : t("rollbackLast")) : null))));

  const pc = await pendingCard(slug, ctx);
  if (pc) root.append(pc);

  /* состояние в кластере */
  let rtTimer = null;
  const rtBody = h("div", {}, loading());
  root.append(card(t("runtime"), rtBody, h("button", { class: "btn small", type: "button", "data-testid": "runtime-refresh", onclick: () => loadRuntime() }, t("refresh"))));
  async function loadRuntime() {
    try {
      const r = await api(`${base}/runtime${qs({ environment: cur.environment })}`);
      if (!r.available) { rtBody.replaceChildren(h("div", { class: "empty", "data-testid": "runtime-error" }, `${t("runtimeUnavailable")}: ${r.error}`)); return; }
      if (r.kind === "server") {
        rtBody.replaceChildren(h("div", { class: "card-body" }, h("dl", { class: "kv" },
          h("dt", {}, t("servers")), h("dd", { "data-testid": "srv-runtime-status" }, `${r.server} `, badge(r.online ? t("srvSt_online") : t("srvSt_offline"), r.online ? "ok" : "bad")),
          h("dt", {}, "OS / Docker"), h("dd", { class: "muted small" }, [r.os, r.docker ? `Docker ${r.docker}` : null].filter(Boolean).join(" · ") || "—"),
          h("dt", {}, t("image")), h("dd", { class: "mono" }, shortImage(r.image))),
          r.containers.length ? tableWrap(th(t("srvContainer"), t("status"), t("image")), h("tbody", {}, r.containers.map((c) => h("tr", { "data-testid": `ctr-${c.name}` },
            h("td", { class: "mono" }, c.name), h("td", {}, badge(c.state, c.state === "running" ? "ok" : "bad"), " ", h("span", { class: "muted small" }, c.status)), h("td", { class: "mono" }, shortImage(c.image)))))) : h("div", { class: "empty" }, t("srvNoContainer"))));
      } else {
      const healthy = r.ready >= r.replicas && r.replicas > 0;
      rtBody.replaceChildren(h("div", { class: "card-body" },
        h("dl", { class: "kv" },
          h("dt", {}, t("replicasLbl")), h("dd", { "data-testid": "replicas" }, badge(`${r.ready} / ${r.replicas}`, healthy ? "ok" : r.ready > 0 ? "warn" : "bad")),
          h("dt", {}, t("availableLbl")), h("dd", {}, `${r.available_replicas} (${r.updated} updated)`),
          h("dt", {}, t("image")), h("dd", { class: "mono" }, shortImage(r.image))),
        r.pods.length ? tableWrap(th(t("podsLbl"), t("phase"), t("status"), t("restarts"), t("started"), ""),
          h("tbody", {}, r.pods.map((p) => h("tr", { "data-testid": `pod-${p.name}` },
            h("td", { class: "mono" }, p.name), h("td", {}, p.phase), h("td", {}, p.ready ? badge(t("podReady"), "ok") : badge(p.reason || t("podNotReady"), "bad")),
            h("td", { class: p.restarts > 0 ? "mono" : "mono muted" }, p.restarts > 0 ? badge(String(p.restarts), "warn") : "0"), h("td", { class: "time" }, fmtTime(p.started)), h("td", { class: "actions" }, pcan("deploy") ? h("button", { class: "btn small", type: "button", "data-testid": `pod-logs-${p.name}`, onclick: () => showPodLogs(p) }, t("logs")) : null))))) : null));
      }
    } catch (e) { rtBody.replaceChildren(errorBox(e)); }
    clearTimeout(rtTimer);   // одна цепочка опроса, даже если нажимали «Обновить»
    if (gen === pollGen) rtTimer = setTimeout(() => { if (gen === pollGen) { if (document.hidden) loadRuntimeLater(); else loadRuntime(); } }, 10000);
  }
  const loadRuntimeLater = () => { clearTimeout(rtTimer); if (gen === pollGen) rtTimer = setTimeout(() => (document.hidden ? loadRuntimeLater() : loadRuntime()), 10000); };
  loadRuntime();

  /* сборки */
  const bBody = h("div", {}, loading());
  root.append(card(t("builds"), bBody, h("button", { class: "btn small", type: "button", onclick: () => loadBuilds() }, t("refresh"))));
  async function loadBuilds() {
    try {
      const r = await api(`${base}/builds${qs({ environment: cur.environment })}`);
      if (!r.available) { bBody.replaceChildren(h("div", { class: "empty" }, `${t("runtimeUnavailable")}: ${r.error}`)); return; }
      if (!r.builds.length) { bBody.replaceChildren(h("div", { class: "empty" }, t("noBuilds"))); return; }
      const kind = { succeeded: "ok", failed: "bad", building: "busy" };
      const label = { succeeded: t("succeededLbl"), failed: t("failedBuild"), building: t("buildingLbl") };
      bBody.replaceChildren(tableWrap(th(t("when"), t("status"), t("revisionShort"), t("detail"), ""),
        h("tbody", {}, r.builds.map((b) => h("tr", { "data-testid": `build-${b.name}` },
          h("td", { class: "time" }, fmtTime(b.created_at)), h("td", {}, badge(label[b.status] || b.status, kind[b.status] || "")),
          h("td", { class: "mono" }, (b.revision || "").slice(0, 10) || "—"), h("td", { class: "mono muted ellipsis", title: b.message }, b.reason || b.message || ""),
          h("td", { class: "actions" }, pcan("deploy") ? h("button", { class: "btn small", type: "button", "data-testid": `logs-${b.name}`, onclick: () => showLogs(b.name) }, t("viewLogs")) : null))))));
      if (r.builds.some((b) => b.status === "building") && gen === pollGen) setTimeout(() => gen === pollGen && loadBuilds(), 8000);
    } catch (e) { bBody.replaceChildren(errorBox(e)); }
  }
  async function showLogs(name) {
    const pre = h("pre", { class: "logs", "data-testid": "logs-text" }, t("loading"));
    const dlg = formDialog(t("logsFor", { n: name }), pre, (close) => [h("button", { class: "btn primary", type: "button", onclick: close }, t("close"))]);
    dlg.classList.add("wide");
    try {
      const res = await api(`${base}/builds/${enc(name)}/logs${qs({ environment: cur.environment })}`, { raw: true });
      pre.textContent = (await res.text()) || t("logsEmpty");
      pre.scrollTop = pre.scrollHeight;
    } catch (e) { pre.textContent = e.message; }
  }
  async function showPodLogs(pod) {
    const pre = h("pre", { class: "logs", "data-testid": "pod-log-text" }, t("loading"));
    const tail = h("select", { "aria-label": t("tailLines"), "data-testid": "pod-log-tail", onchange: () => load() },
      [100, 300, 1000, 2000].map((n) => h("option", { value: String(n), selected: n === 300 }, String(n))));
    const prev = h("input", { type: "checkbox", "data-testid": "pod-log-prev", onchange: () => load() });
    const dlg = formDialog(t("podLogsFor", { n: pod.name }), h("div", { class: "stack" },
      h("div", { class: "row" }, h("label", { class: "row" }, prev, h("span", { class: "small" }, t("prevRun"))), h("span", { class: "spacer" }), h("span", { class: "small" }, t("tailLines")), tail),
      h("p", { class: "muted small" }, t("podLogsNote")), pre),
    (close) => [h("button", { class: "btn", type: "button", onclick: () => load() }, t("refresh")), h("button", { class: "btn primary", type: "button", onclick: close }, t("close"))]);
    dlg.classList.add("wide");
    async function load() {
      pre.textContent = t("loading");
      try {
        const res = await api(`${base}/pods/${enc(pod.name)}/logs${qs({ environment: cur.environment, tail: tail.value, previous: prev.checked ? "true" : "" })}`, { raw: true });
        pre.textContent = (await res.text()) || t("logsEmpty");
        pre.scrollTop = pre.scrollHeight;
      } catch (e) { pre.textContent = e.message; }
    }
    if (pod.restarts > 0 && !pod.ready) prev.checked = true;      // под падает — сначала показываем лог прошлого запуска
    load();
  }
  loadBuilds();

  /* релизы */
  const relBody = h("div", {}, loading());
  root.append(card(t("releases"), relBody, h("button", { class: "btn small", type: "button", onclick: loadReleases }, t("refresh"))));

  async function loadReleases() {
    try {
      const rels = await api(`${base}/releases${qs({ environment: cur.environment })}`);
      if (!rels.length) { relBody.replaceChildren(h("div", { class: "empty" }, t("noReleases"))); return; }
      const firstDeployed = rels.findIndex((r) => r.status === "deployed");
      relBody.replaceChildren(tableWrap(
        th(t("when"), t("status"), t("revisionShort"), t("image"), t("by"), ""),
        h("tbody", {}, rels.map((r, i) => h("tr", {},
          h("td", { class: "time" }, fmtTime(r.created_at)), h("td", {}, releaseBadge(r.status),
            r.error_message ? h("details", { class: "release-error", "data-testid": "release-error" }, h("summary", { class: "muted small" }, t("failureReason")), h("pre", { class: "mono small" }, r.error_message)) : null),
          h("td", { class: "mono" }, (r.git_revision || "").slice(0, 10) || "—"), h("td", { class: "mono" }, shortImage(r.image_digest)),
          h("td", { class: "mono" }, r.triggered_by),
          h("td", { class: "actions" }, pcan("rollback") && r.status === "deployed" && r.image_digest && i !== firstDeployed
            ? h("button", { class: "btn small", type: "button", onclick: () => rollback(r.id) }, t("rollbackHere")) : null))))));
      if (rels.some((r) => r.status === "building") && gen === pollGen) setTimeout(() => gen === pollGen && loadReleases(), 5000);
    } catch (e) { relBody.replaceChildren(errorBox(e)); }
  }

  /** Поля аварийного обхода: только администратору и только если среда требует согласования. */
  function breakGlassFields() {
    if (!(needApproval && ctx.can("users:manage"))) return { node: null, params: () => ({}) };
    const cb = h("input", { type: "checkbox", "data-testid": "bg-check", "aria-label": t("breakGlass") });
    const why = h("input", { "aria-label": t("breakGlassReason"), placeholder: t("breakGlassReason"), "data-testid": "bg-reason" });
    return { node: h("div", { class: "field" }, h("label", {}, cb, ` ${t("breakGlass")}`), why),
      params: () => (cb.checked ? { break_glass: "true", reason: why.value.trim() } : {}) };
  }
  function afterRequest(r, close) {
    close();
    if (r && r.requires_approval) { toast(t("approvalRequested"), "ok"); window.dispatchEvent(new Event("hashchange")); }
    else { toast(t("queued"), "ok"); setTimeout(loadReleases, 1500); }
  }

  function redeployDialog() {
    const rev = h("input", { "aria-label": t("revision"), placeholder: "e7533e4…", "data-testid": "rev-input" });
    const bg = breakGlassFields();
    formDialog(needApproval ? t("requestRedeploy") : t("redeploy"),
      h("div", { class: "field" }, h("label", {}, t("revision")), rev, h("span", { class: "small muted" }, t("redeployHint")),
        needApproval ? h("p", { class: "small", "data-testid": "approval-hint" }, t("approvalNeededHint")) : null, bg.node),
      (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "rev-go", onclick: async () => {
        try {
          const r = await api(`${base}/redeploy${qs({ environment: cur.environment, revision: rev.value.trim(), ...bg.params() })}`, { method: "POST" });
          afterRequest(r, close);
        } catch (e) { toast(e.message, "bad"); }
      } }, needApproval ? t("requestRedeploy") : t("redeploy"))]);
  }

  async function rollback(releaseId) {
    if (!needApproval) {
      if (!(await confirmDialog({ title: t("rollbackLast"), message: t("confirmRollback"), danger: true }))) return;
      try {
        await api(`${base}/rollback${qs({ environment: cur.environment, to: releaseId })}`, { method: "POST" });
        toast(t("rolledBack"), "ok"); loadReleases();
      } catch (e) { toast(e.message, "bad"); }
      return;
    }
    const bg = breakGlassFields();
    formDialog(t("requestRollback"), h("div", { class: "field" }, h("p", {}, t("confirmRollback")), h("p", { class: "small", "data-testid": "approval-hint" }, t("approvalNeededHint")), bg.node),
      (close) => [h("button", { class: "btn danger", type: "button", "data-testid": "rollback-go", onclick: async () => {
        try {
          const r = await api(`${base}/rollback${qs({ environment: cur.environment, to: releaseId, ...bg.params() })}`, { method: "POST" });
          if (r && r.requires_approval) afterRequest(r, close); else { close(); toast(t("rolledBack"), "ok"); loadReleases(); }
        } catch (e) { toast(e.message, "bad"); }
      } }, t("requestRollback"))]);
  }
  loadReleases();

  /* среды */
  const refresh = () => window.dispatchEvent(new Event("hashchange"));
  const envBody = h("div", {}, loading());
  const canManage = ctx.can("projects:manage");
  async function domainDialog(e) {
    const hostIn = h("input", { placeholder: "app.example.com", autocomplete: "off", "data-testid": "domain-host", "aria-label": t("domain") });
    const info = h("div", { "data-testid": "domain-info", class: "small muted" }, t("loading"));
    const out = h("div", { "data-testid": "domain-result" });
    const apply = async (host, close) => {
      try {
        const r = await api(`${base}/environments/${enc(e.name)}/domain`, { method: "PUT", body: { host } });
        toast(r.host ? t("domainSet", { url: r.url }) : t("domainRemoved"), "ok"); close();
      } catch (er) { clear(out); out.append(h("div", { class: "callout bad", role: "alert" }, er.message)); }
    };
    formDialog(`${t("domain")}: ${e.name}`, h("div", { class: "stack" }, h("p", { class: "muted small" }, t("domainHelp")),
      h("div", { class: "field" }, h("label", {}, t("domain")), hostIn), info, out, certSection(e, base)),
    (close) => [h("button", { class: "btn danger", type: "button", "data-testid": "domain-remove", onclick: () => apply(null, close) }, t("domainRemove")),
      h("button", { class: "btn primary", type: "button", "data-testid": "domain-save", onclick: () => apply(hostIn.value.trim(), close) }, t("save"))]);
    try {
      const d = await api(`${base}/environments/${enc(e.name)}/domain`);
      hostIn.value = d.host || "";
      info.textContent = d.error ? t("domainCurrentErr") : `${t("domainHttps")}: ${t(`domainTls_${d.tls}`)}`;
    } catch (er) { info.textContent = er.message; }
  }

  function envDialog(existing) {
    const f = {
      name: h("input", { "aria-label": t("envName"), value: existing ? existing.name : "", disabled: !!existing, "data-testid": "env-name" }),
      ns: h("input", { "aria-label": t("namespace"), value: existing ? existing.namespace : "", "data-testid": "env-ns" }),
      dep: h("input", { "aria-label": t("deployment"), value: existing ? existing.deployment_name : "", "data-testid": "env-dep" }),
      con: h("input", { "aria-label": t("container"), value: existing ? existing.container_name : "", "data-testid": "env-con" }),
      branch: h("input", { "aria-label": t("branch"), value: existing ? existing.branch : "main", "data-testid": "env-branch" }),
      auto: h("select", { "aria-label": t("autoDeploy"), value: existing ? String(existing.auto_deploy) : "true", "data-testid": "env-auto" },
        h("option", { value: "true" }, t("on")), h("option", { value: "false" }, t("off"))),
      appr: h("select", { "aria-label": t("requireApprovalLbl"), value: existing ? String(!!existing.require_approval) : "false", "data-testid": "env-approval" },
        h("option", { value: "false" }, t("off")), h("option", { value: "true" }, t("on"))),
    };
    const field = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);
    const multi = ((ctx.me && ctx.me.features) || []).includes("multi_cluster");
    if (multi) {          // платная функция: выбор кластера среды
      f.cluster = h("select", { "aria-label": t("cluster"), "data-testid": "env-cluster" }, h("option", { value: "local" }, t("clusterLocal")));
      api("/api/clusters").then((cl) => { for (const c of cl) f.cluster.append(h("option", { value: c.name }, c.name)); f.cluster.value = (existing && existing.cluster) || "local"; }).catch(() => {});
    }
    const hasServers = ((ctx.me && ctx.me.features) || []).includes("servers");
    const rt = (existing && existing.runtime) || {};
    const port0 = (rt.ports || [])[0] || {};
    if (hasServers) {          // платная функция: запуск приложения в Docker на сервере с агентом
      f.server = h("select", { "aria-label": t("srvTarget"), "data-testid": "env-server" }, h("option", { value: "local" }, t("srvTargetK8s")));
      f.hport = h("input", { type: "number", "aria-label": t("srvHostPort"), value: port0.host ? String(port0.host) : "", placeholder: "8080", "data-testid": "env-hport" });
      f.cport = h("input", { type: "number", "aria-label": t("srvContainerPort"), value: port0.container ? String(port0.container) : "", placeholder: "8000", "data-testid": "env-cport" });
      f.bind = h("select", { "aria-label": t("srvBind"), "data-testid": "env-bind" }, h("option", { value: "0.0.0.0" }, t("srvBindAll")), h("option", { value: "127.0.0.1" }, t("srvBindLocal")));
      f.bind.value = port0.bind || "0.0.0.0";
      f.hpath = h("input", { "aria-label": t("srvHealthPath"), value: rt.health && rt.health.mode === "http" ? rt.health.path : "", placeholder: "/health", "data-testid": "env-hpath" });
      f.restart = h("select", { "aria-label": t("srvRestart"), "data-testid": "env-restart" }, ["unless-stopped", "always", "on-failure", "no"].map((v) => h("option", { value: v }, v)));
      f.restart.value = rt.restart || "unless-stopped";
      f.memory = h("input", { "aria-label": t("srvMemory"), value: rt.memory || "", placeholder: "512m", "data-testid": "env-memory" });
      f.srvBox = h("div", { class: "form-grid", hidden: true, "data-testid": "env-server-box" }, field(t("srvHostPort"), f.hport), field(t("srvContainerPort"), f.cport), field(t("srvBind"), f.bind),
        field(t("srvHealthPath"), f.hpath), field(t("srvRestart"), f.restart), field(t("srvMemory"), f.memory));
      const syncSrv = () => { f.srvBox.hidden = f.server.value === "local"; if (f.cluster) f.cluster.disabled = f.server.value !== "local"; };
      f.server.addEventListener("change", syncSrv);
      api("/api/servers").then((ss) => { for (const s of ss) f.server.append(h("option", { value: s.name }, `${s.name} (${t(`srvSt_${s.status}`)})`)); f.server.value = (existing && existing.server) || "local"; syncSrv(); }).catch(() => {});
    }
    formDialog(existing ? t("editEnvironment") : t("addEnvironment"),
      h("div", { class: "form-grid" }, field(t("envName"), f.name), field(t("namespace"), f.ns), field(t("deployment"), f.dep),
        field(t("container"), f.con), field(t("branch"), f.branch), field(t("autoDeploy"), f.auto), field(t("requireApprovalLbl"), f.appr), f.cluster ? field(t("cluster"), f.cluster) : null, f.server ? field(t("srvTarget"), f.server) : null, f.srvBox || null),
      (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "env-save", onclick: async () => {
        try {
          const payload = { namespace: f.ns.value.trim(), deployment_name: f.dep.value.trim(), container_name: f.con.value.trim(),
            branch: f.branch.value.trim() || "main", auto_deploy: f.auto.value === "true", require_approval: f.appr.value === "true" };
          if (f.cluster) payload.cluster = f.cluster.value;
          if (f.server) {
            payload.server = f.server.value;
            if (f.server.value !== "local") {
              payload.runtime = { restart: f.restart.value, ...(f.memory.value.trim() ? { memory: f.memory.value.trim() } : {}),
                ports: f.hport.value && f.cport.value ? [{ host: Number(f.hport.value), container: Number(f.cport.value), bind: f.bind.value }] : [],
                health: f.hpath.value.trim() && f.hport.value ? { mode: "http", port: Number(f.hport.value), path: f.hpath.value.trim(), timeout_seconds: 60 } : { mode: "running" } };
              delete payload.cluster;
            }
          }
          if (existing) await api(`${base}/environments/${enc(existing.name)}`, { method: "PATCH", body: payload });
          else await api(`${base}/environments`, { method: "POST", body: { name: f.name.value.trim(), ...payload } });
          close(); toast(existing ? t("envUpdated") : t("envCreated"), "ok"); refresh();
        } catch (e) { toast(e.message, "bad"); }
      } }, t("save"))]);
  }
  async function loadEnvs() {
    try {
      const list = await api(`${base}/environments`);
      envBody.replaceChildren(tableWrap(th(t("envName"), t("namespace"), t("deployment"), t("branch"), t("autoDeploy"), t("approvalRequired"), ""),
        h("tbody", {}, list.map((e) => h("tr", { "data-testid": `env-${e.name}` },
          h("td", {}, h("a", { href: projectHref(slug, e.name) }, e.name)), h("td", { class: "mono" }, e.namespace, e.cluster ? [" ", badge(e.cluster, "pro")] : null, e.server ? [" ", badge(`⛁ ${e.server}`, "pro")] : null, e.preview_of ? [" ", badge(`PR #${e.preview_of}`, "warn")] : null),
          h("td", { class: "mono" }, `${e.deployment_name}/${e.container_name}`), h("td", { class: "mono" }, e.branch),
          h("td", {}, badge(e.auto_deploy ? t("on") : t("off"), e.auto_deploy ? "ok" : "")),
          h("td", {}, e.require_approval ? badge(t("on"), "pro") : badge(t("off"))),
          h("td", { class: "actions" }, ctx.can("deploy") ? [h("button", { class: "btn small", type: "button", "data-testid": `env-conn-${e.name}`, onclick: () => connectionsDialog(e, base) }, t("connCheck")), " "] : null, canManage ? [
            e.server ? null : h("button", { class: "btn small", type: "button", "data-testid": `env-domain-${e.name}`, onclick: () => domainDialog(e) }, t("domain")), " ",
            h("button", { class: "btn small", type: "button", "data-testid": `env-edit-${e.name}`, onclick: () => envDialog(e) }, t("edit")), " ",
            h("button", { class: "btn small danger", type: "button", "data-testid": `env-del-${e.name}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("deleteEnvHint"), danger: true, confirmLabel: t("delete"), expect: e.name }))) return;
              try { await api(`${base}/environments/${enc(e.name)}`, { method: "DELETE" }); toast(t("envDeleted"), "ok");
                location.hash = projectHref(slug, list.find((x) => x.name !== e.name).name); refresh(); } catch (er) { toast(er.message, "bad"); }
            } }, t("delete"))] : null))))));
    } catch (e) { envBody.replaceChildren(errorBox(e)); }
  }
  root.append(card(t("environments"), envBody, (canManage && ctx.me && ctx.me.environments) ? h("button", { class: "btn small primary", type: "button", "data-testid": "env-add", onclick: () => envDialog(null) }, t("addEnvironment")) : null));
  loadEnvs();

  /* источник кода и вебхук */
  {
    const srcBody = h("div", {}, loading());
    root.append(card(t("source"), srcBody));
    (async function loadSource() {
      try {
        const src = await api(`${base}/source`);
        const rows = [h("div", { class: "muted small" }, `${src.provider} · `, h("span", { class: "mono" }, src.clone_url)),
          h("div", {}, t("gitToken"), ": ", src.has_git_token ? badge(t("on"), "ok") : badge(t("off")))];
        rows.push(h("div", {}, t("buildMethod"), ": ", badge(src.dockerfile_generated ? t("dockerfilePlatform") : src.build_method === "dockerfile" ? `Dockerfile (${src.dockerfile_path})` : t("methodBuildpacks"), "pro")));
        if (canManage) {
          const methodSel = h("select", { "aria-label": t("buildMethod"), "data-testid": "src-method" },
            h("option", { value: "buildpacks" }, t("methodBuildpacks")), h("option", { value: "dockerfile" }, "Dockerfile"));
          methodSel.value = src.build_method;
          const dfIn = h("input", { "aria-label": t("dockerfilePath"), value: src.dockerfile_path, "data-testid": "src-dockerfile" });
          rows.push(h("div", { class: "row" }, methodSel, dfIn, h("button", { class: "btn small", type: "button", "data-testid": "src-method-save", onclick: async () => {
            try { await api(base, { method: "PATCH", body: { build_method: methodSel.value, dockerfile_path: dfIn.value.trim() || "Dockerfile" } }); toast(t("policySaved"), "ok"); loadSource(); } catch (e) { toast(e.message, "bad"); }
          } }, t("save"))));
          if (src.dockerfile_generated) {
            const df = await api(`${base}/dockerfile`);
            const ta = h("textarea", { class: "mono dockerfile-text", rows: 16, spellcheck: "false", "aria-label": t("dockerfilePlatform"), "data-testid": "src-dftext" });
            ta.value = df.content || "";
            rows.push(h("details", { open: true }, h("summary", {}, t("dockerfilePlatform")), h("p", { class: "muted small" }, t("dockerfilePlatformHint")), ta,
              h("div", { class: "row" },
                h("button", { class: "btn small primary", type: "button", "data-testid": "src-df-save", onclick: async () => {
                  try { await api(base, { method: "PATCH", body: { dockerfile_content: ta.value } }); toast(t("policySaved"), "ok"); } catch (e) { toast(e.message, "bad"); }
                } }, t("save")),
                h("button", { class: "btn small", type: "button", "data-testid": "src-df-repo", onclick: async () => {
                  try { await api(base, { method: "PATCH", body: { dockerfile_content: "" } }); toast(t("policySaved"), "ok"); loadSource(); } catch (e) { toast(e.message, "bad"); }
                } }, t("useRepoDockerfile")))));
          }
          const tokIn = h("input", { type: "password", "aria-label": t("gitToken"), autocomplete: "new-password", "data-testid": "src-token" });
          rows.push(h("div", { class: "row" }, tokIn, h("button", { class: "btn small", type: "button", "data-testid": "src-token-save", onclick: async () => {
            try { await api(`${base}/git-token`, { method: "PUT", body: { token: tokIn.value.trim() || null } }); toast(t("policySaved"), "ok"); loadSource(); } catch (e) { toast(e.message, "bad"); }
          } }, t("save"))));
          const wh = await api(`${base}/webhook`);
          const url = `${location.origin}${wh.path}`;
          rows.push(h("p", { class: "muted small" }, t("webhookHint", { p: src.provider })),
            h("div", { class: "field" }, h("label", {}, t("webhookUrl")), h("input", { readonly: true, value: url, "data-testid": "wh-url", class: "mono" })),
            wh.secret ? h("div", { class: "field" }, h("label", {}, t("webhookSecret")), h("input", { readonly: true, value: wh.secret, "data-testid": "wh-secret", class: "mono" })) : h("p", { class: "muted small" }, t("webhookGithubSecret")),
            h("div", { class: "muted small" }, `${t("events")}: ${wh.events.join(", ")}`));
        }
        srcBody.replaceChildren(h("div", { class: "card-body" }, rows));
      } catch (e) { srcBody.replaceChildren(errorBox(e)); }
    })();
  }

  /* временные среды под PR (платная функция) */
  if (((ctx.me && ctx.me.features) || []).includes("preview_envs")) {
    const pvBody = h("div", {}, loading());
    root.append(card(t("previews"), pvBody));
    (async function loadPreviews() {
      try {
        const r = await api(`${base}/previews`);
        const envs = await api(`${base}/environments`);
        const en = h("input", { type: "checkbox", "data-testid": "pv-enabled" }); en.checked = !!r.config.enabled;
        const baseSel = h("select", { "aria-label": t("previewBase"), "data-testid": "pv-base" },
          envs.filter((e) => !e.preview_of).map((e) => h("option", { value: e.name }, e.name)));
        if (r.config.base_env) baseSel.value = r.config.base_env;
        const list = r.previews.length ? tableWrap(th("PR", t("branch"), t("deployment"), t("created"), ""), h("tbody", {}, r.previews.map((p) => h("tr", { "data-testid": `pv-${p.pr}` },
          h("td", { class: "mono" }, h("a", { href: projectHref(slug, p.name) }, `#${p.pr}`)), h("td", { class: "mono" }, p.branch), h("td", { class: "mono" }, p.deployment),
          h("td", { class: "time" }, fmtTime(p.created_at)),
          h("td", { class: "actions" }, canManage ? h("button", { class: "btn small danger", type: "button", "data-testid": `pv-del-${p.pr}`, onclick: async () => {
            if (!(await confirmDialog({ title: t("delete"), message: t("previewDeleteConfirm", { n: p.pr }), danger: true, confirmLabel: t("delete") }))) return;
            try { await api(`${base}/previews/${enc(p.pr)}`, { method: "DELETE" }); toast(t("envDeleted"), "ok"); loadPreviews(); } catch (e) { toast(e.message, "bad"); }
          } }, t("delete")) : null))))) : h("div", { class: "empty" }, t("previewNone"));
        pvBody.replaceChildren(h("div", { class: "card-body" }, h("p", { class: "muted small" }, t("previewNote", { n: r.limit, h: r.ttl_hours })),
          canManage ? h("div", { class: "form-grid" }, h("label", { class: "check" }, en, " ", t("previewEnable")), field2(t("previewBase"), baseSel),
            h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "pv-save", onclick: async () => {
              try { await api(`${base}/previews`, { method: "PUT", body: { enabled: en.checked, base_env: baseSel.value } }); toast(t("policySaved"), "ok"); loadPreviews(); } catch (e) { toast(e.message, "bad"); }
            } }, t("save")))) : null), list);
      } catch (e) { pvBody.replaceChildren(errorBox(e)); }
    })();
  }

  /* секреты */
  if (pcan("secrets:list")) {
    const secBody = h("div", {}, loading());
    const keyIn = h("input", { "aria-label": t("key"), placeholder: "DATABASE_PASSWORD", autocomplete: "off", "data-testid": "secret-key" });
    const valIn = h("input", { type: "password", "aria-label": t("value"), autocomplete: "new-password", "data-testid": "secret-value" });
    const scopeSel = h("select", { "aria-label": t("scope"), value: "env", "data-testid": "secret-scope" },
      h("option", { value: "env" }, `${t("scopeThisEnv")} (${cur.environment})`), h("option", { value: "all" }, t("scopeAll")));
    const canWrite = pcan("secrets:write");
    async function historyDialog(sct) {
      const box = h("div", {}, loading());
      const dlg = formDialog(`${t("history")}: ${sct.key}`, box, () => []);
      try {
        const vs = await api(`${base}/secrets/${enc(sct.key)}/versions${qs({ environment: sct.inherited ? "" : sct.scope })}`);
        box.replaceChildren(h("p", { class: "muted small" }, t("historyNote")), tableWrap(th(t("version"), t("when"), t("actor"), t("reason"), ""), h("tbody", {}, vs.map((v) => h("tr", { "data-testid": `secret-ver-${v.version}` },
          h("td", { class: "mono" }, `v${v.version}`), h("td", { class: "time" }, fmtTime(v.created_at)), h("td", { class: "mono" }, v.created_by || ""),
          h("td", {}, badge(v.reason === "restore" ? t("restored") : t("reasonSet"), v.reason === "restore" ? "pro" : "")),
          h("td", { class: "actions" }, v.current ? badge(t("current"), "ok") : canWrite ? h("button", { class: "btn small", type: "button", "data-testid": `secret-restore-${v.version}`, onclick: async () => {
            if (!(await confirmDialog({ title: t("restore"), message: t("confirmRestore", { k: sct.key, v: v.version }), confirmLabel: t("restore") }))) return;
            try { const r = await api(`${base}/secrets/${enc(sct.key)}/restore${qs({ environment: sct.inherited ? "" : sct.scope })}`, { method: "POST", body: { version: v.version } });
              toast(t("secretRestored", { v: r.version }), "ok"); dlg.close(); loadSecrets(); } catch (e) { toast(e.message, "bad"); }
          } }, t("restore")) : null))))));
      } catch (e) { box.replaceChildren(errorBox(e)); }
    }
    function policyDialog(sct) {
      const dateIn = h("input", { type: "date", "aria-label": t("expiresAt"), "data-testid": "policy-expires", value: sct.expires_at ? sct.expires_at.slice(0, 10) : "" });
      const daysIn = h("input", { type: "number", min: "1", max: "3650", "aria-label": t("rotationDays"), "data-testid": "policy-days", value: sct.rotation_days ? String(sct.rotation_days) : "" });
      formDialog(`${t("policy")}: ${sct.key}`, h("div", { class: "form-grid" },
        h("p", { class: "muted small" }, t("policyNote")),
        h("div", { class: "field" }, h("label", {}, t("expiresAt")), dateIn),
        h("div", { class: "field" }, h("label", {}, t("rotationDays")), daysIn)),
      (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "policy-save", onclick: async () => {
        try {
          await api(`${base}/secrets/${enc(sct.key)}/policy${qs({ environment: sct.inherited ? "" : sct.scope })}`, { method: "PUT", body: {
            expires_at: dateIn.value ? `${dateIn.value}T23:59:59Z` : null, rotation_days: daysIn.value ? Number(daysIn.value) : null } });
          toast(t("policySaved"), "ok"); close(); loadSecrets();
        } catch (e) { toast(e.message, "bad"); }
      } }, t("save"))]);
    }
    async function loadSecrets() {
      try {
        const list = await api(`${base}/secrets${qs({ environment: cur.environment })}`);
        if (!list.length) { secBody.replaceChildren(h("div", { class: "empty" }, t("noSecrets"))); return; }
        const envQ = (sct) => qs({ environment: sct.inherited ? "" : sct.scope });
        secBody.replaceChildren(tableWrap(th(t("key"), t("scope"), t("version"), t("status"), t("updated"), ""), h("tbody", {}, list.map((sct) => h("tr", { "data-testid": `secret-${sct.key}` },
          h("td", { class: "mono" }, sct.key),
          h("td", {}, sct.inherited ? badge(t("inherited")) : badge(t("override", { e: sct.scope }), "pro")),
          h("td", { class: "mono", "data-testid": `secret-version-${sct.key}` }, `v${sct.version}`),
          h("td", {}, badge(t(`secStatus_${sct.status}`), sct.status === "ok" ? "ok" : sct.status === "expired" ? "bad" : "warn"),
            sct.expires_at ? h("div", { class: "muted small" }, `${t("expiresAt")}: ${fmtTime(sct.expires_at)}`) : null),
          h("td", { class: "time" }, fmtTime(sct.updated_at), sct.updated_by ? h("div", { class: "muted small" }, sct.updated_by) : null),
          h("td", { class: "actions" },
            h("button", { class: "btn small", type: "button", "data-testid": `secret-history-${sct.key}`, onclick: () => historyDialog(sct) }, t("history")),
            canWrite ? h("button", { class: "btn small", type: "button", "data-testid": `secret-policy-${sct.key}`, onclick: () => policyDialog(sct) }, t("policy")) : null,
            canWrite ? h("button", { class: "btn small danger", type: "button", onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("confirmDeleteSecret", { k: sct.key }), danger: true, confirmLabel: t("delete") }))) return;
              try { await api(`${base}/secrets/${enc(sct.key)}${envQ(sct)}`, { method: "DELETE" });
                toast(t("secretDeleted"), "ok"); loadSecrets(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete")) : null))))));
      } catch (e) { secBody.replaceChildren(errorBox(e)); }
    }
    root.append(card(t("secrets"), h("div", {},
      h("div", { class: "card-body" }, h("p", { class: "muted small" }, `${t("secretsNote")} ${t("secretScopeHint")}`),
        canWrite ? h("div", { class: "form-grid" },
          h("div", { class: "field" }, h("label", {}, t("key")), keyIn), h("div", { class: "field" }, h("label", {}, t("value")), valIn),
          h("div", { class: "field" }, h("label", {}, t("scope")), scopeSel),
          h("div", { class: "row" },
            h("button", { class: "btn primary", type: "button", "data-testid": "secret-set", onclick: async () => {
              if (!keyIn.value.trim()) { toast(t("keyRequired"), "bad"); return; }
              try {
                await api(`${base}/secrets/${enc(keyIn.value.trim())}${qs({ environment: scopeSel.value === "env" ? cur.environment : "" })}`, { method: "PUT", body: { value: valIn.value } });
                keyIn.value = ""; valIn.value = ""; toast(t("secretSaved"), "ok"); loadSecrets();
              } catch (e) { toast(e.message, "bad"); }
            } }, t("setSecret")),
            h("button", { class: "btn", type: "button", "data-testid": "secret-sync", onclick: async () => {
              try { const r = await api(`${base}/secrets/sync${qs({ environment: cur.environment })}`, { method: "POST" }); toast(t("synced", { n: r.synced }), "ok"); } catch (e) { toast(e.message, "bad"); }
            } }, t("syncSecrets")))) : null),
      secBody)));
    if (canWrite) root.append(envImportCard({ slug, environment: cur.environment, onDone: loadSecrets }));
    loadSecrets();
  }

  /* участники */
  root.append(membersCard(slug, ctx));

  /* опасная зона */
  if (ctx.can("projects:manage")) {
    root.append(card(t("dangerZone"), h("div", { class: "card-body row" }, h("p", { class: "muted small" }, t("deleteProjectHint")), h("span", { class: "spacer" }),
      h("button", { class: "btn danger", type: "button", "data-testid": "delete-project", onclick: async () => {
        if (!(await confirmDialog({ title: t("deleteProject"), message: t("deleteProjectHint"), danger: true, confirmLabel: t("delete"), expect: slug }))) return;
        try { await api(base, { method: "DELETE" }); toast(t("projectDeleted"), "ok"); location.hash = "#/projects"; } catch (e) { toast(e.message, "bad"); }
      } }, t("deleteProject")))));
  }
}

/* ───────────── аудит ───────────── */
export async function auditView(root, ctx) {
  const projSel = h("select", { "aria-label": t("filterProject"), "data-testid": "audit-project" }, h("option", { value: "" }, t("all")));
  const actionIn = h("input", { "aria-label": t("filterAction"), placeholder: "deploy, secret_set, access_denied…", "data-testid": "audit-action" });
  const limitSel = h("select", { "aria-label": t("limit"), value: "100" }, ["50", "100", "250", "500"].map((n) => h("option", { value: n }, n)));
  const body = h("div", {}, loading());
  const exportBtn = h("button", { class: "btn", type: "button", hidden: true, "data-testid": "audit-export", onclick: exportCsv }, t("exportCsv"));

  root.append(h("div", { class: "page-head" }, h("h1", {}, t("audit"))),
    card(t("audit"), h("div", {}, h("div", { class: "card-body form-grid" },
      h("div", { class: "field" }, h("label", {}, t("filterProject")), projSel),
      h("div", { class: "field" }, h("label", {}, t("filterAction")), actionIn),
      h("div", { class: "field" }, h("label", {}, t("limit")), limitSel),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: load }, t("refresh")), exportBtn)), body)));

  try { for (const s of new Set((await api("/api/projects")).map((r) => r.slug))) projSel.append(h("option", { value: s }, s)); } catch { /* фильтр по проекту необязателен */ }
  if (ctx.can("license:read")) {
    try { const lic = await api("/api/license"); if ((lic.features || []).includes("audit_export")) exportBtn.hidden = false; } catch { /* нет платного модуля */ }
  }

  const integrity = h("div", { class: "card-body", "data-testid": "audit-integrity" });
  const verifyBtn = h("button", { class: "btn", type: "button", "data-testid": "audit-verify", onclick: verify }, t("verifyChain"));
  root.append(card(t("integrity"), h("div", {}, integrity, h("div", { class: "card-body" }, verifyBtn))));
  async function verify() {
    try {
      const r = await api("/api/audit/verify");
      const lines = [h("div", {}, badge(r.ok ? t("chainOk") : t("chainBroken"), r.ok ? "ok" : "bad"),
        " ", t("chainEvents"), ": ", String(r.events))];
      if (r.head) lines.push(h("div", { class: "mono muted", "data-testid": "audit-head" }, `#${r.head_seq} ${r.head}`), h("div", { class: "muted" }, t("chainHeadHint")));
      if (r.first_problem) lines.push(h("div", { class: "form-error" }, `#${r.first_problem.seq}: ${r.first_problem.reason}`));
      if (r.unchained) lines.push(h("div", { class: "muted" }, `${t("chainUnchained")}: ${r.unchained}`));
      integrity.replaceChildren(...lines);
    } catch (e) { integrity.replaceChildren(errorBox(e)); }
  }

  async function load() {
    try {
      const ev = await api(`/api/audit${qs({ project: projSel.value, action: actionIn.value.trim(), limit: limitSel.value })}`);
      if (!ev.length) { body.replaceChildren(h("div", { class: "empty" }, t("noEvents"))); return; }
      const tbl = tableWrap(th(t("when"), t("actor"), t("action"), t("detail")), h("tbody", {}, ev.map((e) => h("tr", {},
        h("td", { class: "time" }, fmtTime(e.created_at)), h("td", { class: "mono" }, e.actor),
        h("td", {}, badge(e.action, e.action === "access_denied" || e.action === "deploy_failed" ? "bad" : "")),
        h("td", { class: "mono muted", title: JSON.stringify(e.detail) }, JSON.stringify(e.detail || {}).slice(0, 120))))));
      body.replaceChildren(tbl);
    } catch (e) { body.replaceChildren(errorBox(e)); }
  }
  async function exportCsv() {
    try {
      const res = await api(`/api/audit/export${qs({ project: projSel.value })}`, { raw: true });
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: "audit.csv" }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { toast(e.message, "bad"); }
  }
  projSel.addEventListener("change", load); limitSel.addEventListener("change", load);
  load();
}

/* ───────────── токены ───────────── */
export async function tokensView(root) {
  const nameIn = h("input", { "aria-label": t("tokenName"), placeholder: "ci-bot", autocomplete: "off", "data-testid": "token-name" });
  const roleSel = h("select", { "aria-label": t("role"), value: "developer", "data-testid": "token-role" },
    [["admin", t("roleAdmin")], ["devops", t("roleDevops")], ["developer", t("roleDeveloper")], ["viewer", t("roleViewer")]].map(([v, l]) => h("option", { value: v }, l)));
  const projSel = h("select", { "aria-label": t("tokenProject"), "data-testid": "token-project" }, h("option", { value: "" }, t("allProjects")));
  api("/api/projects").then((rows) => { for (const s of new Set(rows.map((r) => r.slug))) projSel.append(h("option", { value: s }, s)); }).catch(() => {});
  projSel.addEventListener("change", () => {   // токен проекта не может быть admin
    const adminOpt = [...roleSel.options].find((o) => o.value === "admin");
    if (adminOpt) adminOpt.disabled = projSel.value !== "";
    if (projSel.value && roleSel.value === "admin") roleSel.value = "devops";
  });
  const body = h("div", {}, loading());

  root.append(h("div", { class: "page-head" }, h("h1", {}, t("tokens"))),
    card(t("createToken"), h("div", { class: "card-body form-grid" },
      h("div", { class: "field" }, h("label", {}, t("tokenName")), nameIn), h("div", { class: "field" }, h("label", {}, t("role")), roleSel),
      h("div", { class: "field" }, h("label", {}, t("tokenProject")), projSel),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "token-create", onclick: async () => {
        try {
          const r = await api("/api/tokens", { method: "POST", body: { name: nameIn.value.trim(), role: roleSel.value, project: projSel.value || undefined } });
          nameIn.value = ""; showSecretOnce({ title: t("tokenCreated"), note: t("tokenOnce"), value: r.token }); load();
        } catch (e) { toast(e.message, "bad"); }
      } }, t("createToken"))))),
    card(t("tokens"), body));

  async function load() {
    try {
      const list = await api("/api/tokens");
      body.replaceChildren(tableWrap(th(t("tokenName"), t("role"), t("tokenProject"), t("prefix"), t("created"), t("lastUsed"), t("status"), ""),
        h("tbody", {}, list.map((k) => h("tr", { "data-testid": `token-${k.name}` },
          h("td", { class: "mono" }, k.name), h("td", {}, badge(k.role, k.role === "admin" ? "pro" : "")), h("td", { class: "mono" }, k.project || "—"), h("td", { class: "mono" }, `${k.prefix}…`),
          h("td", { class: "time" }, fmtTime(k.created_at)), h("td", { class: "time" }, fmtTime(k.last_used_at)),
          h("td", {}, k.revoked ? badge(t("revoked"), "bad") : badge(t("active"), "ok")),
          h("td", { class: "actions" }, k.revoked ? null : h("button", { class: "btn small danger", type: "button", onclick: async () => {
            if (!(await confirmDialog({ title: t("revoke"), message: t("confirmRevoke", { n: k.name }), danger: true, confirmLabel: t("revoke") }))) return;
            try { await api(`/api/tokens/${enc(k.id)}`, { method: "DELETE" }); toast(t("tokenRevoked"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
          } }, t("revoke"))))))));
    } catch (e) { body.replaceChildren(errorBox(e)); }
  }
  load();
}

/* ───────────── лицензия ───────────── */
function limitsText(l) {
  if (!l.limits) return "—";
  const lim = l.limits, usage = l.usage || {};
  const cap = (max, used) => (max == null ? t("unlimited") : max === 0 ? "—" : used == null ? String(max) : `${used} / ${max}`);
  const parts = [`${t("projects")}: ${cap(lim.max_projects, usage.projects)}`, `${t("envsPerProject")}: ${cap(lim.max_environments)}`, `${t("usersLbl")}: ${cap(lim.max_users, usage.users)}`];
  if (lim.max_previews !== 0) parts.push(`${t("previewsLbl")}: ${cap(lim.max_previews)}`);
  if (lim.max_clusters !== 0) parts.push(`${t("clustersLbl")}: ${cap(lim.max_clusters)}`);
  if (lim.max_servers !== 0) parts.push(`${t("serversLbl")}: ${cap(lim.max_servers)}`);
  return parts.join(" · ");
}

const PLANS = [
  { id: "community", price: "$0", rows: [1, 1, 1] }, { id: "lite", price: "$20", rows: [5, 3, 3] },
  { id: "pro", price: "$100", rows: [25, 10, 10] }, { id: "enterprise", price: "$200", rows: [null, null, null] },
];
const lim = (n) => (n == null || n >= 1e6 ? "∞" : String(n));

function meter(label, used, max) {
  const unl = max == null || max >= 1e6, pct = unl ? 0 : Math.min(100, Math.round((used / max) * 100));
  const bar = h("div", { class: "meter-bar" }, h("div", { class: `meter-fill${pct >= 100 ? " full" : pct >= 80 ? " near" : ""}`, "data-pct": String(Math.round(pct / 5) * 5) }));
  return h("div", { class: "meter", "data-testid": "meter" }, h("div", { class: "row" }, h("span", {}, label), h("span", { class: "spacer" }), h("b", { class: "num" }, `${used} / ${lim(max)}`)), unl ? null : bar);
}

export async function licenseView(root) {
  const body = loading();
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("licenseAndPlans"))), body);
  try {
    const l = await api("/api/license");
    const kind = { valid: "ok", none: "", expired: "warn", invalid: "bad" }[l.status] || "";
    const lm = l.limits || {}, us = l.usage || {};
    const usageCard = card(t("usageLbl"), h("div", { class: "card-body", "data-testid": "license-usage" },
      meter(t("projectsLbl"), us.projects || 0, lm.max_projects), meter(t("usersLbl"), us.users || 0, lm.max_users),
      h("div", { class: "small muted" }, `${t("envPerProject")}: ${lim(lm.max_environments)}`)));
    const plans = h("div", { class: "plans", "data-testid": "plans" }, PLANS.map((p) => h("div", { class: `plan${p.id === l.tier ? " current" : ""}` },
      h("div", { class: "row" }, h("b", {}, t(`plan_${p.id}`)), h("span", { class: "spacer" }), p.id === l.tier ? badge(t("currentPlan"), "ok") : null),
      h("div", { class: "plan-price" }, p.price, p.id === "community" ? null : h("span", { class: "muted small" }, ` ${t("perMonth")}`)),
      h("ul", {}, [`${t("projectsLbl")}: ${lim(p.rows[0])}`, `${t("envPerProject")}: ${lim(p.rows[1])}`, `${t("usersLbl")}: ${lim(p.rows[2])}`].map((x) => h("li", {}, x))))));
    const status = h("div", { class: "card-body" }, h("dl", { class: "kv", "data-testid": "license-kv" },
      h("dt", {}, t("licenseStatus")), h("dd", {}, badge(l.status === "none" ? t("none") : l.status, kind)),
      h("dt", {}, t("tier")), h("dd", {}, l.tier === "community" ? t("community") : badge(l.tier, "pro")),
      h("dt", {}, t("customer")), h("dd", {}, l.customer || "—"),
      h("dt", {}, t("expires")), h("dd", {}, l.expires_at || "—"),
      h("dt", {}, t("features")), h("dd", {}, (l.features || []).length ? l.features.map((f) => badge(f, "pro")) : "—"),
      h("dt", {}, t("limitsLbl")), h("dd", { "data-testid": "license-limits" }, limitsText(l)),
      h("dt", {}, t("eeLoaded")), h("dd", {}, l.ee_loaded ? t("yes") : t("no")),
      h("dt", {}, t("installId")), h("dd", {}, h("span", { class: "mono", "data-testid": "install-id" }, l.install_id || "—"),
        l.install_id ? [" ", h("button", { class: "btn small", type: "button", "data-testid": "install-id-copy",
          onclick: async () => { try { await navigator.clipboard.writeText(l.install_id); toast(t("copied"), "ok"); } catch { toast(l.install_id); } } }, t("copy"))] : null,
        h("div", { class: "small muted" }, t("installIdHelp"))),
      h("dt", {}, t("reason")), h("dd", { class: "muted" }, l.reason)));
    body.replaceWith(h("div", { class: "page-inner" }, usageCard, card(t("plansLbl"), h("div", { class: "card-body" }, plans)), card(t("licenseStatus"), status)));
  } catch (e) { body.replaceWith(errorBox(e)); }
}


/* ───────────── обзор ───────────── */
const fmtDuration = (sec) => (sec == null ? "—" : sec < 90 ? `${sec} s` : `${Math.floor(sec / 60)} m ${sec % 60} s`);

export async function overviewView(root, ctx) {
  const daysSel = h("select", { "aria-label": t("period"), value: sessionStorage.getItem("dsp_days") || "30", "data-testid": "ov-period" },
    ["7", "30", "90"].map((n) => h("option", { value: n }, t("daysN", { n }))));
  const body = h("div", { class: "page-inner" }, loading());
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("overview")), h("div", { class: "field" }, h("label", {}, t("period")), daysSel)), body);
  daysSel.addEventListener("change", () => { try { sessionStorage.setItem("dsp_days", daysSel.value); } catch { /* ignore */ } load(); });

  const tile = (label, value, sub, kind = "", testid) =>
    h("div", { class: "tile", "data-testid": testid }, h("div", { class: "tile-label" }, label), h("div", { class: `tile-val ${kind}` }, value), h("div", { class: "tile-sub" }, sub || " "));

  async function load() {
    try {
      const o = await api(`/api/overview${qs({ days: daysSel.value })}`);
      const rate = o.success_rate == null ? null : Math.round(o.success_rate * 100);
      const dayLabel = (iso) => new Date(iso + "T12:00:00Z").toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
      const nodes = [
        h("div", { class: "tiles" },
          tile(t("projects"), String(o.totals.projects), t("environmentsN", { n: o.totals.environments }), "", "tile-projects"),
          tile(t("successRate"), rate == null ? "—" : `${rate}%`, `${t("releasesCount")}: ${o.totals.releases}`, rate == null ? "" : rate >= 95 ? "ok" : rate >= 80 ? "warn" : "bad", "tile-rate"),
          tile(t("avgBuild"), fmtDuration(o.avg_build_seconds), "", "", "tile-build"),
          tile(t("deniedAccess"), String(o.denied_24h), "", o.denied_24h > 0 ? "bad" : "ok", "tile-denied"),
          o.totals.pending_approvals > 0 ? tile(t("awaitingApproval"), String(o.totals.pending_approvals), "", "warn", "tile-approvals") : null,
          tile(t("secretsCount"), String(o.totals.secrets), "", "", "tile-secrets"),
          o.totals.tokens_active == null ? null : tile(t("activeTokens"), String(o.totals.tokens_active), "", "", "tile-tokens")),     // роль без права управлять токенами плитку не видит
        card(t("deploysPerDay"), h("div", { class: "card-body", "data-testid": "chart-deploys" }, columnChart({
          ariaLabel: t("deploysPerDay"),
          data: o.deploys_by_day.map((d) => ({ label: dayLabel(d.date), short: String(Number(d.date.slice(8))), values: [d.deployed, d.failed, d.other] })),
          series: [{ name: t("deployedLbl"), color: "var(--s1)" }, { name: t("failedLbl"), color: "var(--bar-bad)" }, { name: t("otherLbl"), color: "var(--axis)" }] }))),
        h("div", { class: "grid-2c" },
          card(t("byProject"), h("div", { class: "card-body" }, o.by_project.length
            ? hbars({ rows: o.by_project.map((p) => ({ label: p.slug, href: `#/project/${enc(p.slug)}/prod`,
              segments: [{ name: t("deployedLbl"), value: p.deployed, color: "var(--s1)" }, { name: t("failedLbl"), value: p.failed, color: "var(--bar-bad)" }, { name: t("otherLbl"), value: p.other, color: "var(--axis)" }],
              suffix: `${p.deployed + p.failed + p.other}` })) })
            : h("div", { class: "muted" }, t("noData")))),
          card(t("failureReasons"), h("div", { class: "card-body" }, o.failures.length
            ? hbars({ rows: o.failures.map((f) => ({ label: f.reason, segments: [{ name: f.reason, value: f.count, color: "var(--bar-bad)" }], suffix: String(f.count) })) })
            : h("div", { class: "muted" }, t("noFailures"))))),
      ];
      if (o.secret_alerts && o.secret_alerts.length) {
        nodes.push(card(`${t("secretAlerts")} (${o.secret_alerts_total})`, h("div", { class: "feed", "data-testid": "secret-alerts" }, o.secret_alerts.map((a) => h("div", { class: "feed-row" },
          h("a", { href: `#/projects/${a.project}`, class: "mono" }, `${a.project} / ${a.key}`),
          h("span", {}, badge(t(`secStatus_${a.status}`), a.status === "expired" ? "bad" : "warn")),
          h("span", { class: "time small" }, a.expires_at ? fmtTime(a.expires_at) : ""))))));
      }
      if (o.recent) {
        nodes.push(card(t("recentActivity"), o.recent.length
          ? h("div", { class: "feed", "data-testid": "feed" }, o.recent.map((e) => h("div", { class: "feed-row" },
            h("span", { class: "time small" }, fmtTime(e.time)), h("span", { class: "mono ellipsis" }, e.actor),
            h("span", {}, badge(e.action, e.action === "access_denied" || e.action === "deploy_failed" ? "bad" : "")),
            h("span", { class: "mono muted ellipsis", title: JSON.stringify(e.detail) }, JSON.stringify(e.detail).slice(0, 100)))))
          : h("div", { class: "empty" }, t("noEvents")),
        h("a", { class: "card-title", href: "#/audit" }, t("audit"))));
      }
      body.replaceChildren(...nodes);
    } catch (e) { body.replaceChildren(errorBox(e)); }
  }
  load();
}
