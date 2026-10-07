// Экспорт аудита в SIEM (платная функция): назначения syslog / Splunk HEC / HTTP и состояние доставки.
import { api } from "./api.js";
import { h, toast, badge, fmtTime, confirmDialog } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const field = (label, input) => h("div", { class: "field" }, h("label", {}, label), input);

export async function siemView(root) {
  const listBody = h("div", {}, h("div", { class: "muted" }, "…"));
  const nameIn = h("input", { "aria-label": t("channelName"), placeholder: "splunk-prod", autocomplete: "off", "data-testid": "siem-name" });
  const kindSel = h("select", { "aria-label": t("channelKind"), "data-testid": "siem-kind" },
    [["syslog", "Syslog (RFC 5424)"], ["splunk_hec", "Splunk HEC"], ["http_json", "HTTP (NDJSON)"]].map(([v, l]) => h("option", { value: v }, l)));
  const hostIn = h("input", { "aria-label": "host", placeholder: "siem.internal", autocomplete: "off", "data-testid": "siem-host" });
  const portIn = h("input", { type: "number", "aria-label": "port", placeholder: "514", "data-testid": "siem-port" });
  const transSel = h("select", { "aria-label": "transport", "data-testid": "siem-transport" }, ["tcp", "udp", "tls"].map((v) => h("option", { value: v }, v.toUpperCase())));
  const fmtSel = h("select", { "aria-label": "format", "data-testid": "siem-format" }, [["cef", "CEF"], ["json", "JSON"]].map(([v, l]) => h("option", { value: v }, l)));
  const urlIn = h("input", { "aria-label": "URL", placeholder: "https://…", autocomplete: "off", "data-testid": "siem-url" });
  const tokenIn = h("input", { type: "password", "aria-label": "token", autocomplete: "new-password", "data-testid": "siem-token" });
  const hdrNameIn = h("input", { "aria-label": "header", placeholder: "Authorization", autocomplete: "off", "data-testid": "siem-header-name" });
  const hdrValIn = h("input", { type: "password", "aria-label": "header value", autocomplete: "new-password", "data-testid": "siem-header-value" });
  const startSel = h("select", { "aria-label": t("siemStart"), "data-testid": "siem-start" },
    h("option", { value: "now" }, t("siemFromNow")), h("option", { value: "beginning" }, t("siemFromStart")));
  const sysFields = h("div", { class: "form-grid" }, field("Host", hostIn), field("Port", portIn), field(t("siemTransport"), transSel), field(t("siemFormat"), fmtSel));
  const urlFields = h("div", { class: "form-grid" }, field("URL", urlIn));
  const hecFields = h("div", { class: "form-grid" }, field("HEC token", tokenIn));
  const httpFields = h("div", { class: "form-grid" }, field(t("siemHeaderName"), hdrNameIn), field(t("siemHeaderValue"), hdrValIn));
  const sync = () => {
    const k = kindSel.value;
    sysFields.hidden = k !== "syslog"; urlFields.hidden = k === "syslog"; hecFields.hidden = k !== "splunk_hec"; httpFields.hidden = k !== "http_json";
  };
  kindSel.addEventListener("change", sync); sync();

  async function create() {
    const k = kindSel.value;
    const body = { name: nameIn.value.trim(), kind: k, start_from: startSel.value };
    if (k === "syslog") { body.host = hostIn.value.trim(); body.transport = transSel.value; body.format = fmtSel.value; if (portIn.value) body.port = Number(portIn.value); }
    else {
      body.url = urlIn.value.trim();
      if (k === "splunk_hec") body.token = tokenIn.value.trim();
      else if (hdrNameIn.value.trim()) { body.header_name = hdrNameIn.value.trim(); body.header_value = hdrValIn.value; }
    }
    try {
      await api("/api/siem/destinations", { method: "POST", body });
      for (const i of [nameIn, hostIn, portIn, urlIn, tokenIn, hdrNameIn, hdrValIn]) i.value = "";
      toast(t("channelCreated"), "ok"); load();
    } catch (e) { toast(e.message, "bad"); }
  }

  root.append(h("div", { class: "page-head" }, h("h1", {}, "SIEM")),
    card(t("siemDestinations"), listBody),
    card(t("siemNew"), h("div", { class: "card-body form-grid" },
      h("p", { class: "muted small" }, t("siemNote")),
      field(t("channelName"), nameIn), field(t("channelKind"), kindSel), sysFields, urlFields, hecFields, httpFields, field(t("siemStart"), startSel),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "siem-create", onclick: create }, t("create"))))));

  async function patch(d, body, okMsg) {
    try { await api(`/api/siem/destinations/${enc(d.id)}`, { method: "PATCH", body }); if (okMsg) toast(okMsg, "ok"); load(); } catch (e) { toast(e.message, "bad"); }
  }
  async function load() {
    try {
      const list = await api("/api/siem/destinations");
      if (!list.length) { listBody.replaceChildren(h("div", { class: "empty" }, t("siemNone"))); return; }
      listBody.replaceChildren(tableWrap(th(t("channelName"), t("channelKind"), t("target"), t("siemLag"), t("siemSent"), t("status"), ""),
        h("tbody", {}, list.map((d) => h("tr", { "data-testid": `siem-${d.name}` },
          h("td", { class: "mono" }, d.name), h("td", {}, badge(d.kind, "pro"), d.kind === "syslog" ? h("span", { class: "muted small" }, ` ${d.format.toUpperCase()}`) : null),
          h("td", { class: "mono" }, d.target),
          h("td", { class: "mono", "data-testid": `siem-lag-${d.name}` }, String(d.lag)),
          h("td", { class: "mono" }, String(d.sent_total)),
          h("td", {}, !d.enabled ? badge(t("off")) : d.last_error ? badge(t("deliveryError"), "bad") : badge(t("delivered"), "ok"),
            d.last_error ? h("div", { class: "muted small", title: d.last_error }, d.last_error.slice(0, 60)) : null,
            d.last_ok_at ? h("div", { class: "muted small" }, fmtTime(d.last_ok_at)) : null),
          h("td", { class: "actions" },
            h("button", { class: "btn small", type: "button", "data-testid": `siem-test-${d.name}`, onclick: async () => {
              try { const r = await api(`/api/siem/destinations/${enc(d.id)}/test`, { method: "POST" });
                toast(r.ok ? t("testOk") : `${t("testFailed")}: ${r.error}`, r.ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); }
            } }, t("test")),
            h("button", { class: "btn small", type: "button", onclick: () => patch(d, { enabled: !d.enabled }) }, d.enabled ? t("disable") : t("enable")),
            h("button", { class: "btn small", type: "button", onclick: async () => {
              if (await confirmDialog({ title: t("siemResend"), message: t("siemResendConfirm", { n: d.name }), confirmLabel: t("siemResend") })) patch(d, { cursor: 0 }, t("siemResendStarted"));
            } }, t("siemResend")),
            h("button", { class: "btn small danger", type: "button", "data-testid": `siem-del-${d.name}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("confirmDeleteChannel", { n: d.name }), danger: true, confirmLabel: t("delete") }))) return;
              try { await api(`/api/siem/destinations/${enc(d.id)}`, { method: "DELETE" }); toast(t("channelDeleted"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete"))))))));
    } catch (e) { listBody.replaceChildren(h("div", { class: "form-error" }, e.message)); }
  }
  load();
}
