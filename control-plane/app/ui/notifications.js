// Уведомления: каналы webhook / Slack / Telegram и выбор событий.
import { api } from "./api.js";
import { h, toast, badge, fmtTime, confirmDialog, showSecretOnce } from "./dom.js";
import { t, getLang } from "./i18n.js";

const enc = encodeURIComponent;
const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));

export async function notificationsView(root) {
  const listBody = h("div", {}, h("div", { class: "muted" }, "…"));
  const nameIn = h("input", { "aria-label": t("channelName"), placeholder: "ops-alerts", autocomplete: "off", "data-testid": "nc-name" });
  const kindSel = h("select", { "aria-label": t("channelKind"), "data-testid": "nc-kind" },
    [["webhook", "Webhook"], ["slack", "Slack"], ["telegram", "Telegram"]].map(([v, l]) => h("option", { value: v }, l)));
  const urlIn = h("input", { "aria-label": "URL", placeholder: "https://…", autocomplete: "off", "data-testid": "nc-url" });
  const tokenIn = h("input", { type: "password", "aria-label": t("botToken"), autocomplete: "new-password", "data-testid": "nc-token" });
  const chatIn = h("input", { "aria-label": "chat_id", placeholder: "-1001234567890", autocomplete: "off", "data-testid": "nc-chat" });
  const projSel = h("select", { "aria-label": t("tokenProject"), "data-testid": "nc-project" }, h("option", { value: "" }, t("allProjects")));
  const urlField = h("div", { class: "field" }, h("label", {}, "URL"), urlIn);
  const tgFields = h("div", { class: "form-grid", hidden: true }, h("div", { class: "field" }, h("label", {}, t("botToken")), tokenIn),
    h("div", { class: "field" }, h("label", {}, "chat_id"), chatIn));
  const eventsBox = h("div", { class: "checks", "data-testid": "nc-events" });
  let catalog = [];
  const boxes = new Map();

  const syncKind = () => { const tg = kindSel.value === "telegram"; urlField.hidden = tg; tgFields.hidden = !tg; };
  kindSel.addEventListener("change", syncKind);
  api("/api/projects").then((rows) => { for (const s of new Set(rows.map((r) => r.slug))) projSel.append(h("option", { value: s }, s)); }).catch(() => {});

  try {
    catalog = await api("/api/notifications/events");
    for (const ev of catalog) {
      const cb = h("input", { type: "checkbox", "data-testid": `nc-ev-${ev.action}` });
      cb.checked = ev.default;
      boxes.set(ev.action, cb);
      eventsBox.append(h("label", { class: "check" }, cb, " ", getLang() === "ru" ? ev.title_ru : ev.title_en));
    }
  } catch (e) { toast(e.message, "bad"); }

  async function create() {
    const body = { name: nameIn.value.trim(), kind: kindSel.value, project: projSel.value || undefined,
      events: [...boxes].filter(([, cb]) => cb.checked).map(([k]) => k) };
    if (kindSel.value === "telegram") { body.bot_token = tokenIn.value.trim(); body.chat_id = chatIn.value.trim(); } else body.url = urlIn.value.trim();
    try {
      const r = await api("/api/notifications/channels", { method: "POST", body });
      nameIn.value = ""; urlIn.value = ""; tokenIn.value = ""; chatIn.value = "";
      toast(t("channelCreated"), "ok");
      if (r.signing_secret) showSecretOnce({ title: t("signingSecret"), note: t("signingNote"), value: r.signing_secret });
      load();
    } catch (e) { toast(e.message, "bad"); }
  }

  root.append(h("div", { class: "page-head" }, h("h1", {}, t("notifications"))),
    card(t("channels"), listBody),
    card(t("newChannel"), h("div", { class: "card-body form-grid" },
      h("p", { class: "muted small" }, t("notifyNote")),
      h("div", { class: "field" }, h("label", {}, t("channelName")), nameIn),
      h("div", { class: "field" }, h("label", {}, t("channelKind")), kindSel),
      urlField, tgFields,
      h("div", { class: "field" }, h("label", {}, t("tokenProject")), projSel),
      h("div", { class: "field" }, h("label", {}, t("events")), eventsBox),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", "data-testid": "nc-create", onclick: create }, t("create")),
        h("button", { class: "btn", type: "button", "data-testid": "nc-check", onclick: async () => {
          try { const r = await api("/api/notifications/check", { method: "POST" }); toast(t("checksDone", { n: r.raised }), r.chain_ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); }
        } }, t("runChecks"))))));

  async function load() {
    try {
      const list = await api("/api/notifications/channels");
      if (!list.length) { listBody.replaceChildren(h("div", { class: "empty" }, t("noChannels"))); return; }
      listBody.replaceChildren(tableWrap(th(t("channelName"), t("channelKind"), t("target"), t("tokenProject"), t("events"), t("status"), ""),
        h("tbody", {}, list.map((c) => h("tr", { "data-testid": `nch-${c.name}` },
          h("td", { class: "mono" }, c.name), h("td", {}, badge(c.kind, "pro")), h("td", { class: "mono" }, c.target),
          h("td", { class: "mono" }, c.project || "—"),
          h("td", { class: "muted small" }, c.events.includes("*") ? t("all") : String(c.events.length)),
          h("td", {}, !c.enabled ? badge(t("off")) : c.last_status === "error" ? badge(t("deliveryError"), "bad") : c.last_status === "ok" ? badge(t("delivered"), "ok") : badge(t("on")),
            c.last_error ? h("div", { class: "muted small", title: c.last_error }, c.last_error.slice(0, 60)) : null,
            c.last_sent_at ? h("div", { class: "muted small" }, fmtTime(c.last_sent_at)) : null),
          h("td", { class: "actions" },
            h("button", { class: "btn small", type: "button", "data-testid": `nch-test-${c.name}`, onclick: async () => {
              try { const r = await api(`/api/notifications/channels/${enc(c.id)}/test`, { method: "POST" });
                toast(r.ok ? t("testOk") : `${t("testFailed")}: ${r.error}`, r.ok ? "ok" : "bad"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("test")),
            h("button", { class: "btn small", type: "button", onclick: async () => {
              try { await api(`/api/notifications/channels/${enc(c.id)}`, { method: "PATCH", body: { enabled: !c.enabled } }); load(); } catch (e) { toast(e.message, "bad"); }
            } }, c.enabled ? t("disable") : t("enable")),
            h("button", { class: "btn small danger", type: "button", "data-testid": `nch-del-${c.name}`, onclick: async () => {
              if (!(await confirmDialog({ title: t("delete"), message: t("confirmDeleteChannel", { n: c.name }), danger: true, confirmLabel: t("delete") }))) return;
              try { await api(`/api/notifications/channels/${enc(c.id)}`, { method: "DELETE" }); toast(t("channelDeleted"), "ok"); load(); } catch (e) { toast(e.message, "bad"); }
            } }, t("delete"))))))));
    } catch (e) { listBody.replaceChildren(h("div", { class: "form-error" }, e.message)); }
  }
  load();
}
