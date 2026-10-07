// Окно «Проверка подключений»: DNS → TCP → TLS (разбор сертификата) → вход для внешних БД, кэша, S3 и HTTPS-сервисов.
// Учётные данные уходят только в запрос проверки: платформа их не сохраняет и не показывает.
import { api } from "./api.js";
import { h, clear, toast, badge, formDialog } from "./dom.js";
import { t } from "./i18n.js";

const KINDS = ["postgres", "mysql", "redis", "s3", "https", "tcp"];
const PORTS = { postgres: 5432, mysql: 3306, redis: 6379, s3: 443, https: 443, tcp: "" };
const TLS_MODES = ["off", "prefer", "require", "verify-ca", "verify-full"];
const DEFAULT_TLS = { s3: "require", https: "require" };

function field(label, input, hint) {
  return h("div", { class: "field" }, h("label", {}, label), input, hint ? h("div", { class: "small muted" }, hint) : null);
}

function targetForm(n, onRemove) {
  const sel = (opts, labelOf, value) => h("select", {}, opts.map((o) => h("option", { value: o, selected: o === value ? "selected" : null }, labelOf(o))));
  const kind = sel(KINDS, (k) => t(`connKind_${k}`), "postgres");
  const host = h("input", { placeholder: "db.example.com", autocomplete: "off", "data-testid": `conn-host-${n}`, "aria-label": t("connHost") });
  const port = h("input", { type: "number", min: "1", max: "65535", placeholder: "5432", "aria-label": t("connPort") });
  const tls = sel(TLS_MODES, (m) => t(`connTls_${m}`), "off");
  const ca = h("textarea", { rows: "4", placeholder: "-----BEGIN CERTIFICATE-----", "data-testid": `conn-ca-${n}`, spellcheck: "false" });
  const user = h("input", { autocomplete: "off", "aria-label": t("connUser") });
  const pass = h("input", { type: "password", autocomplete: "new-password", "data-testid": `conn-pass-${n}`, "aria-label": t("connPassword") });
  const db = h("input", { autocomplete: "off", "aria-label": t("connDatabase") });
  const ak = h("input", { autocomplete: "off", "aria-label": t("connAccessKey") });
  const sk = h("input", { type: "password", autocomplete: "new-password", "aria-label": t("connSecretKey") });
  const path = h("input", { placeholder: "/", "aria-label": t("connPath") });
  const creds = h("div", { class: "stack" });
  const drawCreds = () => {
    const k = kind.value; clear(creds);
    if (k === "postgres") creds.append(field(t("connUser"), user), field(t("connPassword"), pass), field(t("connDatabase"), db));
    else if (k === "redis") creds.append(field(`${t("connUser")} (${t("connOptional")})`, user), field(t("connPassword"), pass));
    else if (k === "s3") creds.append(field(t("connAccessKey"), ak), field(t("connSecretKey"), sk));
    else if (k === "https") creds.append(field(t("connPath"), path));
  };
  kind.addEventListener("change", () => { port.placeholder = PORTS[kind.value] || ""; tls.value = DEFAULT_TLS[kind.value] || "off"; drawCreds(); });
  drawCreds();
  const el = h("div", { class: "card stack", "data-testid": `conn-target-${n}` },
    h("div", { class: "row" }, field(t("connKind"), kind), field(t("connHost"), host), field(t("connPort"), port), field(t("connTls"), tls),
      onRemove ? h("button", { class: "btn small danger", type: "button", onclick: onRemove }, "×") : null),
    creds,
    h("details", {}, h("summary", {}, t("connCa")), h("p", { class: "muted small" }, t("connCaHelp")), ca));
  const read = () => {
    const k = kind.value;
    const spec = { kind: k, host: host.value.trim(), tls: tls.value };
    if (port.value) spec.port = Number(port.value);
    if (ca.value.trim()) spec.ca_pem = ca.value.trim();
    if (k === "postgres") Object.assign(spec, { user: user.value, password: pass.value, database: db.value });
    if (k === "redis") { if (user.value) spec.user = user.value; if (pass.value) spec.password = pass.value; }
    if (k === "s3") { if (ak.value) spec.access_key = ak.value; if (sk.value) spec.secret_key = sk.value; }
    if (k === "https" && path.value) spec.path = path.value;
    return spec;
  };
  return { el, read };
}

const STEP_OK = new Set(["auth_not_checked", "auth_skipped_no_driver", "tls_unsupported_ignored"]);

function stepLine(s) {
  const note = s.code && (s.ok || STEP_OK.has(s.code));
  const kind = s.ok ? (note ? "warn" : "ok") : "bad";
  const msg = s.code ? t(`conn_${s.code}`, { ...s, names: (s.names || []).join(", "), days_ago: s.days_ago ?? "" }) : null;
  const hint = !s.ok && s.code ? t(`connHint_${s.code}`, { ...s, names: (s.names || []).join(", ") }) : null;
  return h("li", { class: `conn-step ${kind}` },
    h("span", { class: "mono" }, `${s.ok ? "✓" : "✗"} ${t(`connStep_${s.step}`)}`), s.ms != null ? h("span", { class: "muted small" }, ` ${s.ms} ms`) : null,
    s.status ? h("span", { class: "muted small" }, ` HTTP ${s.status}`) : null, s.version ? h("span", { class: "muted small" }, ` ${s.version}`) : null,
    msg ? h("div", { class: s.ok ? "small muted" : "small" }, msg) : null,
    hint ? h("div", { class: "callout", role: "note" }, hint) : null);
}

function certBlock(c) {
  if (!c) return null;
  const names = [...(c.dns_names || []), ...(c.ips || [])].join(", ") || "—";
  const line = (k, v) => h("div", { class: "small" }, h("span", { class: "muted" }, `${k}: `), h("span", { class: "mono" }, v));
  return h("div", { class: "stack small", "data-testid": "conn-cert" },
    line(t("connCertSubject"), c.subject || "—"), line(t("connCertNames"), names), line(t("connCertIssuer"), `${c.issuer || "—"}${c.self_signed ? ` (${t("connSelfSigned")})` : ""}`),
    line(t("connCertExpires"), `${c.not_after.slice(0, 10)} (${c.expired ? t("connExpiredAgo", { n: -c.days_left }) : t("connDaysLeft", { n: c.days_left })})`));
}

function resultCard(r) {
  const warn = (r.warnings || []).filter((w) => w !== "cert_untrusted" || r.target.tls === "require" || r.target.tls === "prefer");
  return h("section", { class: "card", "data-testid": "conn-result" },
    h("div", { class: "card-head" }, h("span", { class: "card-title mono" }, `${t(`connKind_${r.target.kind}`)} ${r.target.host}:${r.target.port}`),
      badge(r.ok ? t("connOk") : t("connFail"), r.ok ? "ok" : "bad")),
    h("ul", { class: "conn-steps" }, r.steps.map(stepLine)),
    warn.length ? h("div", { class: "callout", role: "note" }, warn.map((w) => h("div", {}, `⚠ ${t(`conn_${w}`, { n: r.cert ? r.cert.days_left : "", names: "" })}`))) : null,
    certBlock(r.cert));
}

export function connectionsDialog(env, base) {
  const forms = []; const list = h("div", { class: "stack" }); const out = h("div", { class: "stack", "data-testid": "conn-results", "aria-live": "polite" });
  const add = () => {
    const n = forms.length;
    const f = targetForm(n, forms.length ? () => { forms.splice(forms.indexOf(f), 1); f.el.remove(); } : null);
    forms.push(f); list.append(f.el);
  };
  add();
  const run = async (btn) => {
    clear(out); btn.disabled = true; out.append(h("div", { class: "empty" }, t("connRunning")));
    try {
      const r = await api(`${base}/connections/probe`, { method: "POST", body: { targets: forms.map((f) => f.read()) } });
      clear(out); out.append(...r.results.map(resultCard));
    } catch (er) { clear(out); out.append(h("div", { class: "callout bad", role: "alert" }, er.message)); }
    btn.disabled = false;
  };
  formDialog(`${t("connCheck")}: ${env.name}`,
    h("div", { class: "stack" }, h("p", { class: "muted small" }, t("connHelp")), list,
      h("button", { class: "btn small", type: "button", "data-testid": "conn-add", onclick: add }, `+ ${t("connAddTarget")}`), out),
    (close) => { const go = h("button", { class: "btn primary", type: "button", "data-testid": "conn-run" }, t("connRun")); go.onclick = () => run(go);
      return [go]; });
}
