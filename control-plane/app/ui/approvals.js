// Согласование выкатов и откатов: страница «Согласования» и карточка на странице проекта.
import { api, qs } from "./api.js";
import { h, toast, badge, fmtTime, shortImage, formDialog } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const card = (title, body, actions) =>
  h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, title), actions || null), body);
const tableWrap = (...c) => h("div", { class: "table-wrap" }, h("table", {}, ...c));
const th = (...labels) => h("thead", {}, h("tr", {}, labels.map((l) => h("th", {}, l))));
const KIND = { pending: "warn", approved: "busy", executed: "ok", denied: "bad", failed: "bad", cancelled: "", expired: "" };
const refresh = () => window.dispatchEvent(new Event("hashchange"));

function describe(a) {
  if (a.action === "rollback") return `${t("actionRollback")} → ${shortImage(a.params.image)}`;
  return `${t("actionRedeploy")} ${String(a.params.revision || "").slice(0, 10)}`;
}

function decide(a, verb) {
  const note = h("input", { "aria-label": t("decisionNote"), placeholder: t("decisionNote"), "data-testid": "decision-note" });
  formDialog(`${verb === "approve" ? t("approve") : t("deny")}: ${a.project}/${a.environment}`,
    h("div", { class: "field" }, h("p", { class: "mono" }, describe(a)), h("label", {}, t("decisionNote")), note),
    (close) => [h("button", { class: `btn ${verb === "approve" ? "primary" : "danger"}`, type: "button", "data-testid": `decision-${verb}`, onclick: async () => {
      try {
        await api(`/api/approvals/${enc(a.id)}/${verb}`, { method: "POST", body: { note: note.value.trim() || undefined } });
        close(); toast(verb === "approve" ? t("approved") : t("denied"), "ok"); refresh();
      } catch (e) { toast(e.message, "bad"); }
    } }, verb === "approve" ? t("approve") : t("deny"))]);
}

export function approvalsTable(rows, ctx, { showProject = true } = {}) {
  if (!rows.length) return h("div", { class: "empty", "data-testid": "no-approvals" }, t("noApprovals"));
  return tableWrap(th(t("when"), showProject ? t("projects") : null, t("action"), t("requestedBy"), t("status"), t("decidedBy"), ""),
    h("tbody", {}, rows.map((a) => h("tr", { "data-testid": `approval-${a.id}` },
      h("td", { class: "time" }, fmtTime(a.requested_at)),
      showProject ? h("td", {}, h("a", { href: `#/project/${enc(a.project)}/${enc(a.environment)}` }, `${a.project}/${a.environment}`)) : null,
      h("td", { class: "mono" }, describe(a)), h("td", { class: "mono" }, a.requested_by),
      h("td", {}, badge(t("st_" + a.status), KIND[a.status] || ""), a.error ? h("div", { class: "small muted" }, a.error) : null),
      h("td", { class: "small muted" }, a.decided_by ? `${a.decided_by}${a.note ? `: ${a.note}` : ""}` : "—"),
      h("td", { class: "actions wrap" },
        a.can_decide ? [h("button", { class: "btn small primary", type: "button", "data-testid": "approve", onclick: () => decide(a, "approve") }, t("approve")),
          h("button", { class: "btn small danger", type: "button", "data-testid": "deny", onclick: () => decide(a, "deny") }, t("deny"))] : null,
        a.status === "pending" && (a.requested_by === ctx.me.name || ctx.can("users:manage")) ? h("button", { class: "btn small", type: "button", "data-testid": "cancel-request", onclick: async () => {
          try { await api(`/api/approvals/${enc(a.id)}`, { method: "DELETE" }); toast(t("requestCancelled"), "ok"); refresh(); } catch (e) { toast(e.message, "bad"); }
        } }, t("cancelRequest")) : null)))));
}

export async function approvalsView(root, ctx) {
  const sel = h("select", { "aria-label": t("status"), value: "pending", "data-testid": "approval-filter" },
    h("option", { value: "pending" }, t("st_pending")), h("option", { value: "" }, t("all")));
  const body = h("div", {}, h("div", { class: "empty" }, t("loading")));
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("approvals")), h("div", { class: "field" }, h("label", {}, t("status")), sel)),
    card(t("approvals"), body));
  async function load() {
    try { body.replaceChildren(approvalsTable(await api(`/api/approvals${qs({ status: sel.value })}`), ctx)); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
  }
  sel.addEventListener("change", load);
  load();
}

/** Ожидающие заявки одного проекта (для страницы проекта). Возвращает null, если их нет. */
export async function pendingCard(slug, ctx) {
  let rows = [];
  try { rows = await api(`/api/approvals${qs({ status: "pending", project: slug })}`); } catch { return null; }
  if (!rows.length) return null;
  return card(t("pendingApprovals"), approvalsTable(rows, ctx, { showProject: false }));
}
