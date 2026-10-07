// Удалённые кластеры (платная функция multi_cluster): регистрация, проверка связи, манифест доступа.
import { api, qs } from "./api.js";
import { h, toast, badge, fmtTime, confirmDialog, formDialog } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const field = (label, input) => h("div", { class: "field" }, h("label", {}, label), input);

export async function clustersView(root) {
  const listBody = h("div", {}, h("div", { class: "muted" }, "…"));
  const nameIn = h("input", { "aria-label": t("channelName"), placeholder: "edge-1", autocomplete: "off", "data-testid": "cl-name" });
  const serverIn = h("input", { "aria-label": "server", placeholder: "https://k8s.example.com:6443", autocomplete: "off", "data-testid": "cl-server" });
  const tokenIn = h("input", { type: "password", "aria-label": "token", autocomplete: "new-password", "data-testid": "cl-token" });
  const caIn = h("textarea", { "aria-label": "CA", rows: "4", placeholder: "-----BEGIN CERTIFICATE-----", "data-testid": "cl-ca" });

  async function create() {
    try {
      await api("/api/clusters", { method: "POST", body: { name: nameIn.value.trim(), server: serverIn.value.trim(), token: tokenIn.value.trim(), ca_pem: caIn.value.trim() || undefined } });
      for (const i of [nameIn, serverIn, tokenIn, caIn]) i.value = "";
      toast(t("clusterRegistered"), "ok"); load();
    } catch (e) { toast(e.message, "bad"); }
  }
  function manifestDialog() {
    const nsIn = h("input", { "aria-label": t("namespace"), placeholder: "app-prod,app-stage", "data-testid": "cl-manifest-ns" });
    const out = h("textarea", { readonly: true, rows: "14", class: "mono", "data-testid": "cl-manifest-out" });
    formDialog(t("clusterManifest"), h("div", { class: "form-grid" }, h("p", { class: "muted small" }, t("clusterManifestNote")), field(t("namespace"), nsIn), out),
      () => [h("button", { class: "btn primary", type: "button", "data-testid": "cl-manifest-go", onclick: async () => {
        try { const r = await api(`/api/clusters/access-manifest${qs({ namespaces: nsIn.value.trim() })}`, { raw: true }); out.value = await r.text(); } catch (e) { toast(e.message, "bad"); }
      } }, t("clusterManifestBtn"))]);
  }

  root.append(h("div", { class: "page-head" }, h("h1", {}, t("clusters"))),
    card(t("clusters"), listBody, h("button", { class: "btn small", type: "button", "data-testid": "cl-manifest", onclick: manifestDialog }, t("clusterManifest"))),
    card(t("clusterNew"), h("div", { class: "card-body form-grid" },
      h("p", { class: "muted small" }, t("clusterNote")),
      field(t("channelName"), nameIn), field("Server", serverIn), field("Token", tokenIn), field("CA (PEM)", caIn),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "cl-create", onclick: create }, t("create"))))));

  async function load() {
    try {
      const list = await api("/api/clusters");
      if (!list.length) { listBody.replaceChildren(h("div", { class: "empty" }, t("clusterNone"))); return; }
      listBody.replaceChildren(tableWrap(th(t("channelName"), "Server", t("clusterVersion"), t("environments"), t("clusterChecked"), ""),
        h("tbody", {}, list.map((c) => h("tr", { "data-testid": `cl-${c.name}` },
          h("td", { class: "mono" }, c.name, " ", c.has_ca ? badge("CA", "ok") : badge(t("clusterSystemCa"))), h("td", { class: "mono" }, c.server),
          h("td", { class: "mono", "data-testid": `cl-version-${c.name}` }, c.version || "—"), h("td", {}, String(c.environments)),
          h("td", { class: "time" }, c.last_check ? fmtTime(c.last_check) : "—"),
          h("td", { class: "actions" },
            h("button", { class: "btn small", type: "button", "data-testid": `cl-test-${c.name}`, onclick: async () => {
              try { const r = await api(`/api/clusters/${enc(c.name)}/test`, { method: "POST" });
                toast(r.ok ? `${t("testOk")}: ${r.version}` : `${t("testFailed")}: ${r.error}`, r.ok ? "ok" : "bad"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("test")),
            h("button", { class: "btn small danger", type: "button", "data-testid": `cl-del-${c.name}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("confirmDeleteChannel", { n: c.name }), danger: true, confirmLabel: t("delete") }))) return;
              try { await api(`/api/clusters/${enc(c.name)}`, { method: "DELETE" }); toast(t("channelDeleted"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete"))))))));
    } catch (e) { listBody.replaceChildren(h("div", { class: "form-error" }, e.message)); }
  }
  load();
}
