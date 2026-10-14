/* Сервисы данных среды: БД и S3-хранилища в namespace проекта — версия, миграция, копирование S3. */
import { api } from "./api.js";
import { h, toast, badge, confirmDialog, formDialog } from "./dom.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const ACTIVE = ["creating", "copying"];
const field = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);

export function dataServicesCard(slug, envName, ctx) {
  const base = `/api/projects/${enc(slug)}/environments/${enc(envName)}/data-services`;
  const canManage = ctx.can("projects:manage");
  const body = h("div", { class: "card-body", "data-testid": "data-services" }, h("div", { class: "empty" }, t("loading")));
  let timer = null;
  const root = h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("dataServices"))), body);

  const call = async (path, opts, okMsg) => {
    try { const r = await api(`${base}${path}`, opts); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "bad"); return null; }
  };

  function versionDialog(s) {
    const tag = h("input", { "aria-label": t("dsTag"), value: s.tag, "data-testid": "ds-tag" });
    const backup = h("input", { type: "checkbox", checked: true, "data-testid": "ds-backup" });
    formDialog(`${t("dsChangeVersion")}: ${s.name}`, h("div", { class: "form-grid" },
      h("p", { class: "muted small" }, s.category === "database" ? t("dsVersionHintDb") : t("dsVersionHint")),
      field(t("dsTag"), tag), s.category === "database" ? h("label", { class: "small" }, backup, " ", t("dsBackupFirst")) : null),
    (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "ds-version-go", onclick: async () => {
      const r = await call(`/${enc(s.name)}/version`, { method: "POST", body: { tag: tag.value.trim(), backup: backup.checked } }, t("dsVersionChanged"));
      if (r) { close(); load(); }
    } }, t("save"))]);
  }

  function migrateDialog(s) {
    const tag = h("input", { "aria-label": t("dsTargetTag"), placeholder: s.engine === "postgres" ? "17" : "8.4", "data-testid": "ds-target" });
    const stop = h("input", { type: "checkbox", "data-testid": "ds-stop" });
    formDialog(`${t("dsMigrate")}: ${s.name} (${s.label} ${s.major || s.tag})`, h("div", { class: "form-grid" },
      h("p", { class: "muted small" }, t("dsMigrateHint")), field(t("dsTargetTag"), tag), h("label", { class: "small" }, stop, " ", t("dsStopApp"))),
    (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "ds-migrate-go", onclick: async () => {
      const r = await call(`/${enc(s.name)}/migrate`, { method: "POST", body: { target_tag: tag.value.trim(), stop_app: stop.checked } }, t("dsMigrationStarted"));
      if (r) { close(); load(); }
    } }, t("dsStart"))]);
  }

  function s3Dialog(s) {
    const f = { ep: h("input", { placeholder: "http://seaweedfs:8333", "data-testid": "ds-ep" }), ak: h("input", { autocomplete: "off", "data-testid": "ds-ak" }),
      sk: h("input", { type: "password", autocomplete: "new-password", "data-testid": "ds-sk" }), pr: h("input", { placeholder: "SeaweedFS / Minio / AWS / Other", value: "Other" }) };
    formDialog(`${t("dsS3Copy")}: ${s.name}`, h("div", { class: "form-grid" }, h("p", { class: "muted small" }, t("dsS3Hint")),
      field("Endpoint", f.ep), field("Access key", f.ak), field("Secret key", f.sk), field("Provider", f.pr)),
    (close) => [h("button", { class: "btn primary", type: "button", "data-testid": "ds-s3-go", onclick: async () => {
      const r = await call(`/${enc(s.name)}/s3-copy`, { method: "POST", body: { endpoint: f.ep.value.trim(), access_key: f.ak.value.trim(),
        secret_key: f.sk.value, provider: f.pr.value.trim() || null } }, t("dsMigrationStarted"));
      if (r) { close(); load(); }
    } }, t("dsStart"))]);
  }

  function migrationPanel(s) {
    const m = s.migration;
    const kind = { creating: "busy", copying: "busy", copied: "ok", switched: "ok", failed: "bad", rolled_back: "warn" }[m.phase] || "";
    const btn = (label, onclick, cls = "") => h("button", { class: `btn small ${cls}`, type: "button", onclick }, label);
    const act = [];
    if (canManage) {
      if (ACTIVE.includes(m.phase)) act.push(btn(t("dsRefresh"), () => load()));
      if (m.phase === "copied") act.push(btn(t("dsCutover"), async () => {
        if (!(await confirmDialog({ title: t("dsCutover"), message: t("dsCutoverHint"), confirmLabel: t("dsCutover") }))) return;
        if (await call(`/${enc(s.name)}/migration/cutover`, { method: "POST" }, t("dsSwitched"))) load();
      }, "primary"));
      if (["creating", "copying", "copied", "failed", "switched"].includes(m.phase)) act.push(btn(t("dsRollback"), async () => {
        if (!(await confirmDialog({ title: t("dsRollback"), message: t("dsRollbackHint"), danger: true, confirmLabel: t("dsRollback") }))) return;
        if (await call(`/${enc(s.name)}/migration/rollback`, { method: "POST" }, t("dsRolledBack"))) load();
      }));
      if (m.phase === "switched") act.push(btn(t("dsFinalize"), async () => {
        if (!(await confirmDialog({ title: t("dsFinalize"), message: t("dsFinalizeHint"), danger: true, confirmLabel: t("delete"), expect: s.name }))) return;
        if (await call(`/${enc(s.name)}/migration/finalize`, { method: "POST", body: { confirm: s.name } }, t("dsFinalized"))) load();
      }, "danger"));
    }
    return h("div", { class: "row", "data-testid": `ds-migration-${s.name}` }, badge(m.phase, kind), " ",
      h("span", { class: "small" }, m.kind === "s3" ? `→ ${m.dest_endpoint}` : `${m.from_tag} → ${m.to_tag}`), " ", h("span", { class: "muted small" }, m.message || ""),
      h("span", { class: "spacer" }), act, m.log ? h("pre", { class: "mono small", style: "width:100%;max-height:12em;overflow:auto" }, m.log) : null);
  }

  async function load() {
    clearTimeout(timer);
    let data;
    try { data = await api(base); } catch (e) {
      body.replaceChildren(h("div", { class: "empty" }, /Kubernetes/.test(e.message || "") ? t("dsK8sOnly") : e.status === 403 ? t("dsNoRights") : `${t("error")}: ${e.message || e.status || "?"}`));
      return;
    }
    // каждую активную миграцию продвигаем (автомат шагает по запросу состояния)
    for (const s of data.services) {
      if (s.migration && (ACTIVE.includes(s.migration.phase) || s.migration.phase === "failed") && canManage) {
        try { s.migration = await api(`${base}/${enc(s.name)}/migration`); } catch (e) { /* покажем последнее известное */ }
      }
    }
    if (!root.isConnected) return;
    if (!data.services.length) { body.replaceChildren(h("div", { class: "empty" }, t("dsNone"))); return; }
    body.replaceChildren(...data.services.map((s) => h("div", { class: "ds-item", "data-testid": `ds-${s.name}` },
      h("div", { class: "row" }, h("strong", {}, s.name), " ", badge(s.label, "pro"), " ", h("span", { class: "mono small" }, s.image), " ",
        badge(`${s.ready}/${s.replicas ?? "?"}`, s.ready >= (s.replicas || 1) ? "ok" : "warn"), " ",
        s.volumes.map((v) => h("span", { class: "muted small" }, `${v.name} ${v.size || ""} `)), h("span", { class: "spacer" }),
        canManage && !(s.migration && ACTIVE.includes(s.migration.phase)) ? [
          h("button", { class: "btn small", type: "button", "data-testid": `ds-version-${s.name}`, onclick: () => versionDialog(s) }, t("dsChangeVersion")), " ",
          s.category === "database" && s.workload === "StatefulSet" ? h("button", { class: "btn small", type: "button", "data-testid": `ds-migrate-${s.name}`, onclick: () => migrateDialog(s) }, t("dsMigrate")) : null,
          s.category === "s3" ? h("button", { class: "btn small", type: "button", "data-testid": `ds-s3-${s.name}`, onclick: () => s3Dialog(s) }, t("dsS3Copy")) : null] : null),
      s.migration && !["finalized"].includes(s.migration.phase) ? migrationPanel(s) : null)));
    if (data.services.some((s) => s.migration && ACTIVE.includes(s.migration.phase))) timer = setTimeout(load, 5000);
  }
  load();
  return root;
}
