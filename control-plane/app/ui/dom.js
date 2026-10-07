// Мини-хелпер построения DOM. Весь текст вставляется как текст (textContent), никогда как HTML.
import { t } from "./i18n.js";

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "value") value = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  if (value !== undefined) el.value = value; // после детей: иначе у <select> нет option
  return el;
}

export const clear = (el) => el.replaceChildren();

export function toast(message, kind = "") {
  const box = document.getElementById("toasts");
  const node = h("div", { class: `toast ${kind}` }, message);
  box.append(node);
  setTimeout(() => node.remove(), kind === "bad" ? 8000 : 4500);
}

export function badge(text, kind = "") { return h("span", { class: `badge ${kind}` }, text); }

export function fmtTime(iso) {
  if (!iso) return t("never");
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString();
}

export const shortImage = (image) => {
  if (!image) return "—";
  const m = String(image).match(/^(.*?)(?:@sha256:([0-9a-f]{12})[0-9a-f]*)?$/);
  return m && m[2] ? `${m[1].split("/").slice(-1)[0]}@${m[2]}` : String(image);
};

function openDialog(content) {
  const dlg = h("dialog", {}, content);
  document.body.append(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
  return dlg;
}

/** Диалог подтверждения; expect — строка, которую нужно ввести (для необратимых действий). */
export function confirmDialog({ title, message, confirmLabel, danger = false, expect = null }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } dlg.close(); };
    const input = expect ? h("input", { type: "text", "aria-label": expect, autocomplete: "off" }) : null;
    const ok = h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", onclick: () => finish(true), disabled: !!expect }, confirmLabel || t("confirm"));
    if (input) input.addEventListener("input", () => { ok.disabled = input.value !== expect; });
    const dlg = openDialog(h("div", {},
      h("div", { class: "card-head" }, h("span", { class: "card-title" }, title)),
      h("div", { class: "card-body" },
        h("p", {}, message),
        expect ? h("p", { class: "small muted" }, t("typeToConfirm", { s: expect })) : null,
        input,
        h("div", { class: "row" }, h("span", { class: "spacer" }),
          h("button", { class: "btn", type: "button", onclick: () => finish(false) }, t("cancel")), ok))));
    dlg.addEventListener("close", () => finish(false));
  });
}

/** Показывает значение один раз (новый токен) с кнопкой копирования. */
export function showSecretOnce({ title, note, value }) {
  const copyBtn = h("button", { class: "btn", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(value); toast(t("copied"), "ok"); } catch { /* буфер недоступен — можно выделить вручную */ }
  } }, t("copy"));
  const dlg = openDialog(h("div", {},
    h("div", { class: "card-head" }, h("span", { class: "card-title" }, title)),
    h("div", { class: "card-body" },
      h("p", { class: "muted" }, note),
      h("div", { class: "secret-box mono", "data-testid": "secret-once" }, value),
      h("div", { class: "row" }, h("span", { class: "spacer" }), copyBtn,
        h("button", { class: "btn primary", type: "button", onclick: () => dlg.close() }, t("close"))))));
}

/** Диалог с произвольным содержимым и кнопками; возвращает dialog. */
export function formDialog(title, body, actions) {
  const dlg = openDialog(h("div", {},
    h("div", { class: "card-head" }, h("span", { class: "card-title" }, title)),
    h("div", { class: "card-body" }, body, h("div", { class: "row" }, h("span", { class: "spacer" }),
      h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, t("cancel")), ...actions(() => dlg.close())))));
  return dlg;
}
