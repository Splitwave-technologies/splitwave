// Серверы с агентом (платная функция servers): VPS, серверы в ЦОД и у партнёров. Подключение одной командой, состояние, задания.
import { api } from "./api.js";
import { h, toast, badge, fmtTime, confirmDialog, formDialog, showSecretOnce } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const field = (label, input) => h("div", { class: "field" }, h("label", {}, label), input);
const KIND = { online: "ok", offline: "bad", pending: "warn" };

export async function serversView(root) {
  const listBody = h("div", {}, h("div", { class: "muted" }, "…"));
  const nameIn = h("input", { "aria-label": t("channelName"), placeholder: "vps-moscow-1", autocomplete: "off", "data-testid": "srv-name" });

  function showInstall(res, title) {
    const cmd = h("textarea", { readonly: true, rows: "3", class: "mono", "data-testid": "srv-install-cmd" }); cmd.value = res.install_command;
    formDialog(title, h("div", { class: "form-grid" }, h("p", { class: "muted small" }, t("srvInstallNote")), cmd,
      h("p", { class: "muted small" }, `${t("srvTokenExpires")}: ${fmtTime(res.enroll_expires_at)}`)),
    (close) => [h("button", { class: "btn", type: "button", "data-testid": "srv-copy", onclick: async () => { try { await navigator.clipboard.writeText(res.install_command); toast(t("copied"), "ok"); } catch { cmd.select(); } } }, t("copy")),
      h("button", { class: "btn primary", type: "button", onclick: close }, t("close"))]);
  }

  async function create() {
    try {
      const r = await api("/api/servers", { method: "POST", body: { name: nameIn.value.trim() } });
      nameIn.value = ""; load(); showInstall(r, t("srvConnect"));
    } catch (e) { toast(e.message, "bad"); }
  }

  function registryDialog(s) {
    const f = { server: h("input", { "aria-label": t("srvRegistry"), placeholder: "ghcr.io", "data-testid": "srv-reg-server" }),
      user: h("input", { "aria-label": t("username"), autocomplete: "off", "data-testid": "srv-reg-user" }),
      pass: h("input", { type: "password", "aria-label": t("password"), autocomplete: "new-password", "data-testid": "srv-reg-pass" }) };
    formDialog(`${t("srvRegistry")}: ${s.name}`, h("div", { class: "form-grid" }, h("p", { class: "muted small" }, t("srvRegistryNote")),
      field(t("srvRegistry"), f.server), field(t("username"), f.user), field(t("password"), f.pass)),
    (close) => [h("button", { class: "btn primary", type: "button", onclick: async () => {
      try { await api(`/api/servers/${enc(s.name)}/registry`, { method: "PUT", body: { server: f.server.value.trim(), username: f.user.value.trim(), password: f.pass.value } }); toast(t("policySaved"), "ok"); close(); load(); }
      catch (e) { toast(e.message, "bad"); } } }, t("save")),
      s.has_registry ? h("button", { class: "btn danger", type: "button", onclick: async () => { try { await api(`/api/servers/${enc(s.name)}/registry`, { method: "DELETE" }); close(); load(); } catch (e) { toast(e.message, "bad"); } } }, t("delete")) : null]);
  }

  async function jobsDialog(s) {
    const body = h("div", {}, t("loading"));
    formDialog(`${t("srvJobs")}: ${s.name}`, body, (close) => [h("button", { class: "btn primary", type: "button", onclick: close }, t("close"))]);
    try {
      const jobs = await api(`/api/servers/${enc(s.name)}/jobs`);
      body.replaceChildren(jobs.length ? tableWrap(th(t("when"), t("srvJobKind"), t("status"), t("srvJobResult")), h("tbody", {}, jobs.map((j) => h("tr", {},
        h("td", { class: "time" }, fmtTime(j.created_at)), h("td", {}, j.kind), h("td", {}, badge(j.status, j.status === "succeeded" ? "ok" : j.status === "running" || j.status === "pending" ? "busy" : "bad")),
        h("td", { class: "mono small" }, j.error || (j.log || []).slice(-1)[0] || ""))))) : h("div", { class: "empty" }, t("srvNoJobs")));
    } catch (e) { body.replaceChildren(h("div", { class: "form-error" }, e.message)); }
  }

  root.append(h("div", { class: "page-head" }, h("h1", {}, t("servers"))),
    card(t("servers"), listBody),
    card(t("srvNew"), h("div", { class: "card-body form-grid" }, h("p", { class: "muted small" }, t("srvNote")),
      field(t("channelName"), nameIn), h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "srv-create", onclick: create }, t("srvAdd"))))));

  async function load() {
    try {
      const list = await api("/api/servers");
      if (!list.length) { listBody.replaceChildren(h("div", { class: "empty" }, t("srvNone"))); return; }
      listBody.replaceChildren(tableWrap(th(t("channelName"), t("status"), "OS / Docker", t("srvResources"), t("environments"), t("srvSeen"), ""),
        h("tbody", {}, list.map((s) => h("tr", { "data-testid": `srv-${s.name}` },
          h("td", { class: "mono" }, s.name), h("td", {}, badge(t(`srvSt_${s.status}`), KIND[s.status] || "")),
          h("td", { class: "muted small" }, [s.os, s.docker ? `Docker ${s.docker}` : null].filter(Boolean).join(" · ") || "—"),
          h("td", { class: "muted small" }, s.cpus ? `${s.cpus} CPU · ${s.mem_mb} MB${s.disk_free_gb != null ? ` · ${s.disk_free_gb} GB` : ""}` : "—"),
          h("td", {}, String(s.environments)), h("td", { class: "time" }, s.last_seen_at ? fmtTime(s.last_seen_at) : "—"),
          h("td", { class: "actions" },
            s.status === "pending" ? h("button", { class: "btn small", type: "button", "data-testid": `srv-token-${s.name}`, onclick: async () => {
              try { showInstall(await api(`/api/servers/${enc(s.name)}/enroll-token`, { method: "POST" }), t("srvConnect")); } catch (e) { toast(e.message, "bad"); } } }, t("srvNewToken")) : null,
            h("button", { class: "btn small", type: "button", onclick: () => jobsDialog(s) }, t("srvJobs")),
            h("button", { class: "btn small", type: "button", "data-testid": `srv-registry-${s.name}`, onclick: () => registryDialog(s) }, s.has_registry ? `${t("srvRegistry")} ✓` : t("srvRegistry")),
            h("button", { class: "btn small danger", type: "button", "data-testid": `srv-del-${s.name}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("srvDeleteConfirm", { n: s.name }), danger: true, confirmLabel: t("delete") }))) return;
              try { await api(`/api/servers/${enc(s.name)}`, { method: "DELETE" }); toast(t("channelDeleted"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete"))))))));
    } catch (e) { listBody.replaceChildren(h("div", { class: "form-error" }, e.message)); }
  }
  load();
  const timer = setInterval(() => { if (!document.body.contains(listBody)) clearInterval(timer); else if (!document.hidden) load(); }, 10000);
}
