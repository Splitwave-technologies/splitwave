// Импорт переменных из .env в секреты: файл с компьютера / вставленный текст / файл из репозитория проекта.
// Значения на экране не показываются: файл читается в память, предпросмотр содержит только имена и действия.
import { api, qs } from "./api.js";
import { h, clear, toast, badge } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const field = (label, el, hint) => h("div", { class: "field" }, h("label", {}, label), el, hint ? h("div", { class: "small muted" }, hint) : null);
const callout = (kind, ...c) => h("div", { class: `callout ${kind}`, role: kind === "bad" ? "alert" : null }, ...c);
const KIND = { create: "ok", update: "warn", same: "", exists: "", not_selected: "" };

export function envImportCard({ slug, environment, onDone }) {
  const st = { content: "", fileName: "", mode: "upload", preview: null, picked: new Set() };
  const out = h("div", { "data-testid": "envimp-result" });

  const text = h("textarea", { rows: 5, spellcheck: false, autocomplete: "off", placeholder: "KEY=value\nexport OTHER=\"quoted value\"", "data-testid": "envimp-text", class: "mono",
    oninput: () => { st.content = text.value; reset(); } });
  const fileName = h("span", { class: "chip", hidden: true, "data-testid": "envimp-filename" });
  const fileIn = h("input", { type: "file", accept: ".env,.txt,text/plain,application/octet-stream", "data-testid": "envimp-file", onchange: async () => {
    const f = fileIn.files[0];
    if (!f) return;
    if (f.size > 256 * 1024) { toast(t("envTooBig"), "bad"); fileIn.value = ""; return; }
    st.content = await f.text(); st.fileName = f.name; text.value = ""; text.disabled = true; fileName.textContent = `${f.name} (${f.size} B)`; fileName.hidden = false; clear2.hidden = false; reset();
  } });
  const clear2 = h("button", { class: "btn small", type: "button", hidden: true, "data-testid": "envimp-clearfile", onclick: () => {
    st.content = ""; st.fileName = ""; fileIn.value = ""; text.disabled = false; text.value = ""; fileName.hidden = true; clear2.hidden = true; reset();
  } }, t("envClearFile"));

  const path = h("input", { value: ".env", "aria-label": t("envRepoPath"), "data-testid": "envimp-path", autocomplete: "off", oninput: () => reset() });
  const ref = h("input", { placeholder: t("envRefPh"), "aria-label": t("envRef"), "data-testid": "envimp-ref", autocomplete: "off", oninput: () => reset() });
  const token = h("input", { type: "password", autocomplete: "off", placeholder: t("envTokenPh"), "aria-label": t("gitToken"), "data-testid": "envimp-token" });
  const scope = h("select", { "aria-label": t("scope"), "data-testid": "envimp-scope", onchange: () => reset() },
    h("option", { value: "env" }, t("envScopeEnv", { env: environment })), h("option", { value: "all" }, t("envScopeAll")));
  const overwrite = h("input", { type: "checkbox", "data-testid": "envimp-overwrite", onchange: () => reset() });

  const uploadBox = h("div", { class: "stack", "data-testid": "envimp-upload" }, field(t("envPaste"), text), h("div", { class: "row" }, fileIn, fileName, clear2));
  const repoBox = h("div", { class: "stack", "data-testid": "envimp-repo", hidden: true },
    callout("warn", t("envRepoWarn")),
    h("div", { class: "form-grid" }, field(t("envRepoPath"), path), field(t("envRef"), ref), field(`${t("gitToken")} (${t("optional")})`, token, t("envTokenHint"))));
  const modeSel = h("select", { "aria-label": t("envSource"), "data-testid": "envimp-mode", onchange: () => {
    st.mode = modeSel.value; uploadBox.hidden = st.mode !== "upload"; repoBox.hidden = st.mode !== "repo"; reset();
  } }, h("option", { value: "upload" }, t("envSrcUpload")), h("option", { value: "repo" }, t("envSrcRepo")));

  const previewBtn = h("button", { class: "btn", type: "button", "data-testid": "envimp-preview", onclick: () => run(true) }, t("envPreview"));
  const applyBtn = h("button", { class: "btn primary", type: "button", hidden: true, "data-testid": "envimp-apply", onclick: () => run(false) }, t("envApply"));

  function reset() { st.preview = null; applyBtn.hidden = true; clear(out); }

  function body(dry) {
    const b = { overwrite: overwrite.checked, dry_run: dry };
    if (st.mode === "repo") { b.from_repo = true; b.path = path.value.trim() || ".env"; if (ref.value.trim()) b.ref = ref.value.trim(); if (token.value.trim()) b.token = token.value.trim(); }
    else b.content = st.content;
    if (!dry) b.keys = [...st.picked];
    return b;
  }

  async function run(dry) {
    if (st.mode === "upload" && !st.content.trim()) { clear(out); out.append(callout("bad", t("envNeedContent"))); return; }
    previewBtn.disabled = applyBtn.disabled = true;
    try {
      const r = await api(`/api/projects/${enc(slug)}/secrets/import${qs({ environment: scope.value === "env" ? environment : "" })}`, { method: "POST", body: body(dry) });
      if (dry) { st.preview = r; st.picked = new Set(r.items.filter((i) => i.action === "create" || i.action === "update").map((i) => i.key)); draw(r); }
      else {
        clear(out); out.append(callout("ok", h("span", { "data-testid": "envimp-done" }, t("envDone", { created: r.counts.create, updated: r.counts.update }))));
        st.content = ""; text.value = ""; text.disabled = false; fileIn.value = ""; fileName.hidden = true; clear2.hidden = true; token.value = ""; applyBtn.hidden = true;
        toast(t("envDone", { created: r.counts.create, updated: r.counts.update }), "ok");
        if (onDone) onDone();
      }
    } catch (e) { clear(out); out.append(callout("bad", e.message)); }
    previewBtn.disabled = applyBtn.disabled = false;
  }

  function draw(r) {
    clear(out);
    const rows = r.items.map((i) => {
      const writable = i.action === "create" || i.action === "update";
      const cb = h("input", { type: "checkbox", disabled: !writable, "aria-label": i.key, "data-testid": `envimp-pick-${i.key}`, onchange: () => {
        cb.checked ? st.picked.add(i.key) : st.picked.delete(i.key); applyBtn.disabled = st.picked.size === 0;
      } });
      cb.checked = writable && st.picked.has(i.key);
      return h("tr", { "data-testid": `envimp-row-${i.key}` }, h("td", {}, cb), h("td", { class: "mono" }, i.key),
        h("td", {}, badge(t(`envAct_${i.action}`), KIND[i.action] || "")), h("td", { class: "small muted" }, i.notes.map((n) => t(`envNote_${n}`)).join(", ")));
    });
    const probs = r.problems.map((p) => h("li", { "data-testid": "envimp-problem" }, p.line ? t("envProbLine", { line: p.line, reason: t(`envProb_${p.reason}`), key: p.key || "" }) : t(`envProb_${p.reason}`)));
    out.append(...[
      r.items.length ? h("div", { class: "table-wrap" }, h("table", { class: "table", "data-testid": "envimp-table" },
        h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, t("key")), h("th", {}, t("status")), h("th", {}, ""))), h("tbody", {}, rows))) : callout("warn", t("envNothing")),
      probs.length ? callout("warn", h("div", {}, t("envProblems")), h("ul", {}, probs)) : null,
      h("p", { class: "small muted", "data-testid": "envimp-summary" }, t("envSummary", { create: r.counts.create, update: r.counts.update, skip: r.counts.same + r.counts.exists, bad: r.counts.invalid }))].filter(Boolean));
    applyBtn.hidden = !st.picked.size; applyBtn.disabled = false;
  }

  return h("section", { class: "card", "data-testid": "envimp" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("envImport"))),
    h("div", { class: "card-body stack" }, h("p", { class: "muted small" }, t("envImportNote")),
      h("div", { class: "form-grid" }, field(t("envSource"), modeSel), field(t("scope"), scope), field(t("envOverwrite"), h("label", { class: "row" }, overwrite, h("span", { class: "small" }, t("envOverwriteHint"))))),
      uploadBox, repoBox, h("div", { class: "row" }, previewBtn, applyBtn), out));
}
