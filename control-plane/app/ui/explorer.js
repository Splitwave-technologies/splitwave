// Исследователь событий: строка запроса, диапазон времени, гистограмма, частые значения полей, таблица с раскрытием.
import { api, qs } from "./api.js";
import { h, badge, fmtTime } from "./dom.js";
import { t } from "./i18n.js";
import { columnChart } from "./charts.js";

const RANGES = ["1h", "24h", "7d", "30d", "90d"];

export async function explorerView(root) {
  let range = "24h", offset = 0;
  const PAGE = 50;
  const input = h("input", { class: "q", "data-testid": "ex-query", "aria-label": t("explorerQuery"), placeholder: "action:deploy* project:shop -actor:ci-bot result:failed", spellcheck: "false" });
  const seg = h("div", { class: "seg", role: "group" }, RANGES.map((r) => h("button", { type: "button", "aria-pressed": String(r === range), "data-range": r, onclick: () => { range = r; offset = 0; for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.range === r)); run(); } }, r)));
  const err = h("div", { class: "error-text", "data-testid": "ex-error" });
  const summary = h("div", { class: "muted small", "data-testid": "ex-summary" });
  const chartBox = h("div", { class: "card-body" });
  const side = h("div", { class: "ex-side" });
  const body = h("div", {});
  const pager = h("div", { class: "row card-body" });

  const addCond = (f, v) => { input.value = `${input.value.trim()} ${f}:${/\s/.test(v) ? `"${v}"` : v}`.trim(); offset = 0; run(); };
  const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); offset = 0; run(); } }, input, h("button", { class: "btn primary", type: "submit", "data-testid": "ex-run" }, t("explorerSearch")), seg);
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("explorer"))),
    h("div", { class: "card" }, h("div", { class: "card-body" }, form, err, summary), chartBox),
    h("div", { class: "ex-grid" }, h("div", { class: "card" }, body, pager), side));

  async function run() {
    err.textContent = "";
    try {
      const r = await api(`/api/explorer/events${qs({ q: input.value.trim(), range, limit: PAGE, offset })}`);
      summary.textContent = `${r.total} ${t("explorerEvents")}${r.capped ? ` (${t("explorerCapped")})` : ""}`;
      const data = r.histogram.buckets.map((b) => {
        const d = new Date(b.t); const day = r.histogram.bucket_seconds >= 86400;
        return { label: d.toLocaleString(undefined, day ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }), short: day ? String(d.getDate()) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }), values: [b.n - b.failed, b.failed] };
      });
      chartBox.replaceChildren(columnChart({ data, series: [{ name: t("explorerOk"), color: "var(--s1)" }, { name: t("explorerFailed"), color: "var(--bar-bad)" }], height: 150 }));
      side.replaceChildren(...Object.entries(r.fields).filter(([, v]) => v.length).map(([f, vals]) => h("div", { class: "card" },
        h("div", { class: "card-head" }, h("span", { class: "card-title" }, t(`explorerField_${f}`))),
        h("div", { class: "card-body ex-vals" }, vals.map((x) => h("button", { type: "button", class: "ex-val", title: t("explorerAdd"), onclick: () => addCond(f, x.value) }, h("span", { class: "ellipsis" }, x.value), h("span", { class: "muted" }, String(x.count))))))));
      if (!r.events.length) body.replaceChildren(h("div", { class: "empty" }, t("noEvents")));
      else body.replaceChildren(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, [t("when"), t("actor"), t("action"), t("filterProject"), t("detail")].map((x) => h("th", {}, x)))),
        h("tbody", {}, r.events.map((e) => h("tr", {}, h("td", { class: "time" }, fmtTime(e.time)), h("td", { class: "mono" }, e.actor), h("td", {}, badge(e.action, e.result === "failed" ? "bad" : "")),
          h("td", {}, e.project || "—"), h("td", { class: "mono muted ellipsis ex-detail", title: JSON.stringify(e.detail) }, JSON.stringify(e.detail || {}))))))));
      pager.replaceChildren(
        h("button", { class: "btn small", type: "button", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - PAGE); run(); } }, t("prev")),
        h("button", { class: "btn small", type: "button", disabled: offset + PAGE >= r.total, onclick: () => { offset += PAGE; run(); } }, t("next")),
        h("span", { class: "muted small" }, `${Math.min(offset + 1, r.total)}–${Math.min(offset + PAGE, r.total)} / ${r.total}`));
    } catch (e) { err.textContent = e.message || String(e); }
  }
  await run();
}
