// Блок «Собственный сертификат» в окне домена: загрузка цепочки PEM и ключа (ключ хранится только в кластере и не показывается).
import { api } from "./api.js";
import { h, clear, toast, badge } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;

function fileInto(textarea) {
  const input = h("input", { type: "file", accept: ".pem,.crt,.cer,.key,.txt", class: "small" });
  input.addEventListener("change", async () => { const f = input.files && input.files[0]; if (f && f.size <= 70000) textarea.value = await f.text(); input.value = ""; });
  return input;
}

export function certSection(env, base) {
  const info = h("div", { class: "stack small", "data-testid": "cert-info" }, t("loading"));
  const out = h("div", { "data-testid": "cert-result" });
  const cert = h("textarea", { rows: "5", placeholder: "-----BEGIN CERTIFICATE-----", spellcheck: "false", "data-testid": "cert-pem", "aria-label": t("certPem") });
  const key = h("textarea", { rows: "4", placeholder: "-----BEGIN PRIVATE KEY-----", spellcheck: "false", autocomplete: "off", "data-testid": "cert-key", "aria-label": t("certKey") });
  const url = `${base}/environments/${enc(env.name)}/domain/certificate`;
  const line = (k, v) => h("div", {}, h("span", { class: "muted" }, `${k}: `), h("span", { class: "mono" }, v));
  const warnLine = (w) => h("div", { class: "callout", role: "note" }, `⚠ ${t(`certWarn_${w}`)}`);

  const show = (c) => {
    clear(info);
    if (!c || c.error) { info.append(c && c.error ? t("certUnknown") : "—"); return; }
    if (c.source === "custom") {
      const names = [...(c.dns_names || []), ...(c.ips || [])].join(", ") || "—";
      info.append(h("div", {}, badge(t("certSource_custom"), "ok")), line(t("connCertNames"), names), line(t("connCertIssuer"), c.issuer || "—"),
        line(t("connCertExpires"), `${c.not_after.slice(0, 10)} (${t("connDaysLeft", { n: c.days_left })})`),
        ...(c.days_left < 30 ? [warnLine("cert_expires_soon")] : []));
    } else info.append(h("div", {}, badge(t(`certSource_${c.source}`))));
  };
  const load = async () => { try { show(await api(url)); } catch (er) { clear(info); info.append(er.message); } };
  const explain = (er) => {
    const code = er.detail && er.detail.error;
    return code ? t(`certErr_${code}`, { msg: er.detail.message || "" }) : er.message;
  };
  const upload = async (btn) => {
    clear(out); btn.disabled = true;
    try {
      const r = await api(url, { method: "PUT", body: { certificate: cert.value.trim(), private_key: key.value.trim() } });
      cert.value = ""; key.value = "";                       // ключ не остаётся в форме
      toast(t("certUploaded"), "ok"); show(r);
      (r.warnings || []).forEach((w) => out.append(warnLine(w)));
    } catch (er) { out.append(h("div", { class: "callout bad", role: "alert" }, explain(er))); }
    btn.disabled = false;
  };
  const remove = async () => {
    clear(out);
    try { await api(url, { method: "DELETE" }); toast(t("certRemoved"), "ok"); load(); }
    catch (er) { out.append(h("div", { class: "callout bad", role: "alert" }, explain(er))); }
  };
  const up = h("button", { class: "btn primary small", type: "button", "data-testid": "cert-upload" }, t("certUpload"));
  up.onclick = () => upload(up);
  load();
  return h("details", { class: "stack", "data-testid": "cert-section" }, h("summary", {}, t("certTitle")),
    h("p", { class: "muted small" }, t("certHelp")), info,
    h("div", { class: "field" }, h("label", {}, t("certPem")), cert, fileInto(cert)),
    h("div", { class: "field" }, h("label", {}, t("certKey")), key, fileInto(key)),
    h("div", { class: "row" }, up, h("button", { class: "btn small danger", type: "button", "data-testid": "cert-remove", onclick: remove }, t("certRemove"))), out);
}
