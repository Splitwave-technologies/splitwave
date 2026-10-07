// Мастер подключения проекта: ссылка на репозиторий → проверка предложенных настроек → результат (что сделано, что осталось).
import { api } from "./api.js";
import { h, clear, badge, toast, confirmDialog } from "./dom.js";
import { t } from "./i18n.js";
import { envImportCard } from "./envimport.js";

const enc = encodeURIComponent;
const field = (label, el, hint) => h("div", { class: "field" }, h("label", {}, label), el, hint ? h("div", { class: "small muted" }, hint) : null);
const callout = (kind, ...children) => h("div", { class: `callout ${kind}`, role: kind === "bad" ? "alert" : null }, ...children);
const warnText = (w) => { const k = `warn_${w.code}`; const s = t(k); return s === k ? w.message : s; };

async function copyText(value) {
  try { await navigator.clipboard.writeText(value); toast(t("copied"), "ok"); } catch { /* буфер недоступен — текст можно выделить вручную */ }
}

function stepper(active) {
  const names = [t("wizStep1"), t("wizStep2"), t("wizStep3")];
  return h("ol", { class: "steps", "data-testid": "wiz-steps" }, names.map((n, i) =>
    h("li", { class: i + 1 === active ? "on" : i + 1 < active ? "done" : "" }, `${i + 1}. ${n}`)));
}

export async function onboardingView(root, ctx) {
  const st = { url: "", token: "", provider: "", proposal: null, caps: null };
  const mount = h("div", { class: "stack" });
  root.append(h("div", { class: "page-head" }, h("h1", {}, t("wizTitle")),
    h("a", { class: "btn", href: "#/projects", "data-testid": "wiz-cancel" }, t("cancel"))), mount);

  /* ───── шаг 1: ссылка ───── */
  function stepUrl(error) {
    clear(mount);
    const url = h("input", { class: "big-input", "aria-label": t("wizUrl"), placeholder: "https://github.com/owner/repo", value: st.url, "data-testid": "wiz-url", autocomplete: "off" });
    const token = h("input", { type: "password", "aria-label": t("gitToken"), autocomplete: "new-password", value: st.token, "data-testid": "wiz-token" });
    const provider = h("select", { "aria-label": t("provider"), "data-testid": "wiz-provider" },
      [["", t("wizAuto")], ["github", "GitHub"], ["gitlab", "GitLab"], ["bitbucket", "Bitbucket"], ["gitea", "Gitea / Forgejo"]].map(([v, l]) => h("option", { value: v }, l)));
    provider.value = st.provider;
    const err = h("div", { "data-testid": "wiz-error" });
    if (error) err.append(callout("bad", error));
    const go = h("button", { class: "btn primary", type: "submit", "data-testid": "wiz-inspect" }, t("wizCheck"));
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      st.url = url.value.trim(); st.token = token.value.trim(); st.provider = provider.value;
      if (!st.url) { clear(err); err.append(callout("bad", t("wizNeedUrl"))); return; }
      go.disabled = true; go.textContent = t("wizChecking"); clear(err);
      try {
        const r = await api("/api/onboarding/inspect", { method: "POST", body: { url: st.url, token: st.token || undefined, provider: st.provider || undefined } });
        st.proposal = r.proposal; st.caps = r.capabilities;
        stepReview();
      } catch (ex) { go.disabled = false; go.textContent = t("wizCheck"); err.append(callout("bad", ex.message)); }
    } },
    h("div", { class: "card-body" },
      h("p", { class: "muted" }, t("wizIntro")),
      field(t("wizUrl"), url),
      h("details", {}, h("summary", {}, t("wizPrivate")),
        h("div", { class: "form-grid" }, field(`${t("gitToken")} (${t("optional")})`, token, t("wizTokenHint")), field(t("provider"), provider))),
      err,
      h("div", { class: "row" }, go, h("span", { class: "spacer" }), h("a", { href: "#/projects" }, t("wizManual")))));
    mount.append(stepper(1), h("section", { class: "card" }, form));
    url.focus();
  }

  /* ───── шаг 2: настройки ───── */
  function stepReview() {
    clear(mount);
    const p = st.proposal, caps = st.caps;
    const f = {
      slug: h("input", { "aria-label": t("slug"), value: p.slug, "data-testid": "wiz-slug" }),
      env: h("input", { "aria-label": t("envName"), value: "prod", "data-testid": "wiz-env" }),
      branch: h("input", { "aria-label": t("branch"), value: p.branch, "data-testid": "wiz-branch" }),
      method: h("select", { "aria-label": t("buildMethod"), "data-testid": "wiz-method" },
        p.dockerfile_template ? h("option", { value: "generated" }, t("methodGenerated")) : null,
        h("option", { value: "repo" }, t("methodRepoDockerfile")), h("option", { value: "buildpacks" }, t("methodBuildpacks"))),
      dockerfile: h("input", { "aria-label": t("dockerfilePath"), value: p.dockerfile_path, "data-testid": "wiz-dockerfile" }),
      subPath: h("input", { "aria-label": t("subPath"), value: p.sub_path, "data-testid": "wiz-subpath" }),
      port: h("input", { type: "number", min: 1, max: 65535, "aria-label": t("port"), value: p.port, "data-testid": "wiz-port" }),
      ns: h("input", { "aria-label": t("namespace"), value: p.slug, "data-testid": "wiz-namespace" }),
      host: h("input", { "aria-label": t("wizHost"), placeholder: "app.example.com", "data-testid": "wiz-host" }),
      cluster: h("select", { "aria-label": t("clusters"), "data-testid": "wiz-cluster" }, caps.clusters.map((c) => h("option", { value: c }, c === "local" ? t("wizLocalCluster") : c))),
      server: h("select", { "aria-label": t("servers"), "data-testid": "wiz-server" }, caps.servers.map((s) => h("option", { value: s }, s))),
      autoDeploy: h("input", { type: "checkbox", checked: true, "data-testid": "wiz-autodeploy", "aria-label": t("wizAutoDeploy") }),
      approval: h("input", { type: "checkbox", "data-testid": "wiz-approval", "aria-label": t("wizApproval") }),
      manual: h("input", { type: "checkbox", "data-testid": "wiz-manual", "aria-label": t("wizManualMode") }),
    };
    f.method.value = p.dockerfile_generated ? "generated" : p.build_method === "dockerfile" ? "repo" : "buildpacks";
    f.slug.addEventListener("input", () => { f.ns.value = f.slug.value; });     // namespace по умолчанию = имя проекта
    const target = { kind: "cluster" };
    const targetBox = h("div", { class: "stack" });
    const syncMethod = () => {
      const m = f.method.value;
      f.dockerfile.closest(".field").hidden = m !== "repo";
      if (tplCard) tplCard.hidden = m !== "generated";
      clear(bpBox);
      if (m === "buildpacks") (p.buildpacks_warnings || []).forEach((w) => bpBox.append(callout("warn", warnText(w))));
      f.port.readOnly = m === "generated";                       // порт задаётся в карточке Dockerfile и должен совпадать с ним
      if (m === "generated" && tpl) f.port.value = tp.port.value;
    };
    f.method.addEventListener("change", syncMethod);

    const provisionBox = h("div", { "data-testid": "wiz-provision-info" });
    let lastAccess = { allowed: false, missing: [] };
    const showProvision = (access) => {
      lastAccess = access;
      clear(provisionBox);
      provisionBox.append(f.manual.checked ? callout("warn", t("wizManualChosen")) : access.allowed
        ? callout("ok", t("wizProvisionOk"))
        : callout("warn", h("div", {}, t("wizProvisionNo")), h("details", {}, h("summary", {}, t("wizWhy")), h("p", { class: "small" }, t("wizWhyText")), h("div", { class: "chips" }, (access.missing || []).map((m) => h("span", { class: "chip" }, m))))));
    };
    const loadAccess = async () => {
      if (target.kind !== "cluster") return;
      const cl = f.cluster.value;
      try { showProvision(cl === "local" ? caps.provision.local : await api(`/api/onboarding/access?cluster=${enc(cl)}&ingress=true`)); }
      catch { showProvision({ allowed: false, missing: [] }); }
    };
    f.cluster.addEventListener("change", loadAccess);
    f.manual.addEventListener("change", () => showProvision(lastAccess));

    const drawTarget = () => {
      clear(targetBox);
      const radios = h("div", { class: "row radios" },
        h("label", {}, h("input", { type: "radio", name: "wiz-target", checked: target.kind === "cluster", "data-testid": "wiz-target-cluster", onchange: () => { target.kind = "cluster"; drawTarget(); } }), " Kubernetes"),
        caps.servers.length ? h("label", {}, h("input", { type: "radio", name: "wiz-target", checked: target.kind === "server", "data-testid": "wiz-target-server", onchange: () => { target.kind = "server"; drawTarget(); } }), ` ${t("wizServerTarget")}`) : null);
      if (target.kind === "cluster") {
        targetBox.append(radios,
          h("div", { class: "form-grid" }, caps.clusters.length > 1 ? field(t("clusters"), f.cluster) : null, field(t("namespace"), f.ns), field(`${t("wizHost")} (${t("optional")})`, f.host, t("wizHostHint"))),
          provisionBox);
        loadAccess();
      } else {
        targetBox.append(radios, h("div", { class: "form-grid" }, field(t("servers"), f.server)), callout("ok", t("wizServerInfo", { port: f.port.value })));
      }
    };
    drawTarget();

    const err = h("div", { "data-testid": "wiz-error" });
    const lim = caps.limits || {}, used = (caps.usage || {}).projects || 0;
    const limitHit = !!lim.enforced && lim.max_projects != null && used >= lim.max_projects && !p.existing_project;
    const create = h("button", { class: "btn primary", type: "button", "data-testid": "wiz-create", disabled: !!p.existing_project || limitHit, onclick: async () => {
      clear(err); create.disabled = true; create.textContent = t("wizCreating");
      const body = { slug: f.slug.value.trim(), repo_full_name: p.repo_full_name, provider: p.provider, git_url: p.git_url || undefined,
        git_token: st.token || undefined, sub_path: f.subPath.value.trim(), build_method: f.method.value === "buildpacks" ? "buildpacks" : "dockerfile",
        dockerfile_path: f.dockerfile.value.trim() || "Dockerfile", branch: f.branch.value.trim() || "main", environment: f.env.value.trim() || "prod",
        port: Number(f.port.value) || 8080, auto_deploy: f.autoDeploy.checked, require_approval: f.approval.checked, head_sha: p.head_sha || undefined };
      if (f.method.value === "generated") { body.dockerfile_content = tplText.value; body.dockerfile_path = "Dockerfile"; }
      if (f.manual.checked && target.kind === "cluster") body.provision = false;       // не создавать самим — выдать YAML
      if (target.kind === "server") body.server = f.server.value;
      else { body.namespace = f.ns.value.trim() || undefined; body.host = f.host.value.trim() || undefined; if (f.cluster.value !== "local") body.cluster = f.cluster.value; }
      try { const r = await api("/api/onboarding/create", { method: "POST", body }); stepDone(r, body); }
      catch (ex) { create.disabled = false; create.textContent = t("wizCreate"); err.append(callout("bad", ex.message)); }
    } }, t("wizCreate"));

    const tpl = p.dockerfile_template;
    let tplEdited = false;
    const bpBox = h("div", { class: "stack", "data-testid": "wiz-bp-warnings" });
    const tplText = h("textarea", { class: "mono dockerfile-text", rows: 18, spellcheck: "false", "aria-label": t("dockerfilePlatform"), "data-testid": "wiz-dftext" });
    tplText.value = tpl ? tpl.content : "";
    tplText.addEventListener("input", () => { tplEdited = true; });
    const prm = tpl ? tpl.params : {};
    const tp = {
      port: h("input", { type: "number", min: 1, max: 65535, value: prm.port, "aria-label": t("port"), "data-testid": "wiz-tpl-port" }),
      cmd: "start_command" in prm ? h("input", { value: prm.start_command, "aria-label": t("startCommand"), "data-testid": "wiz-tpl-cmd" }) : null,
      ver: "runtime_version" in prm ? h("input", { value: prm.runtime_version, "aria-label": t("runtimeVersion"), "data-testid": "wiz-tpl-version" }) : null,
    };
    if (tpl && (tpl.language === "static" || (tpl.facts && tpl.facts.static_site))) tp.port.readOnly = true;      // nginx всегда на 8080
    tp.port.addEventListener("input", () => { f.port.value = tp.port.value; });
    const tplNotes = h("div", { class: "stack", "data-testid": "wiz-tpl-notes" });
    const showNotes = (notes) => { clear(tplNotes); (notes || []).forEach((n) => tplNotes.append(callout("warn", n))); };
    showNotes(tpl && tpl.notes);
    const regen = async () => {
      if (tplEdited && !(await confirmDialog({ title: t("wizRegen"), message: t("wizRegenWarn"), confirmLabel: t("wizRegen") }))) return;
      try {
        const params = { port: Number(tp.port.value) || undefined };
        if (tp.cmd) params.start_command = tp.cmd.value;
        if (tp.ver) params.runtime_version = tp.ver.value;
        const r = await api("/api/onboarding/dockerfile", { method: "POST", body: { language: tpl.language, facts: tpl.facts, params } });
        tplText.value = r.content; tplEdited = false; showNotes(r.notes);
        tp.port.value = r.params.port; f.port.value = r.params.port;
        if (tp.cmd) tp.cmd.value = r.params.start_command;
        if (tp.ver) tp.ver.value = r.params.runtime_version;
      } catch (ex) { toast(ex.message, "bad"); }
    };
    const tplCard = tpl ? h("section", { class: "card", "data-testid": "wiz-tpl-card" },
      h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizTplTitle"))),
      h("div", { class: "card-body" }, h("p", { class: "muted small" }, t("wizTplHint")), tplNotes,
        h("div", { class: "form-grid" }, field(t("port"), tp.port), tp.cmd ? field(t("startCommand"), tp.cmd) : null, tp.ver ? field(t("runtimeVersion"), tp.ver) : null,
          h("div", { class: "field" }, h("label", {}, "\u00a0"), h("button", { class: "btn", type: "button", "data-testid": "wiz-tpl-regen", onclick: regen }, t("wizRegen")))),
        tplText)) : null;

    const found = h("div", { class: "row", "data-testid": "wiz-found" },
      badge(p.provider, "pro"), h("span", { class: "mono" }, p.repo_full_name), badge(`${t("branch")}: ${p.branch}`),
      p.language ? badge(p.framework ? `${p.language} · ${p.framework}` : p.language, "ok") : badge(t("wizUnknownStack"), "warn"),
      p.private ? badge(t("wizPrivateBadge"), "warn") : null, h("span", { class: "mono muted" }, (p.head_sha || "").slice(0, 7)));
    const methodNote = p.dockerfile_generated ? t("wizWhyGenerated") : p.build_method === "dockerfile" ? t("wizWhyDockerfile", { path: p.sub_path ? `${p.sub_path}/${p.dockerfile_path}` : p.dockerfile_path }) : t("wizWhyBuildpacks");

    mount.append(stepper(2),
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizFound"))),
        h("div", { class: "card-body" }, found, h("p", { class: "muted small", "data-testid": "wiz-method-note" }, methodNote),
          p.existing_project ? callout("bad", t("wizExists", { slug: p.existing_project }), " ", h("a", { href: `#/project/${enc(p.existing_project)}/prod` }, t("open"))) : null,
          limitHit ? callout("bad", h("span", { "data-testid": "wiz-limit" }, t("limitReached_projects", { max: lim.max_projects, tier: lim.tier })), " ", h("a", { href: "#/license" }, t("license"))) : null,
          p.warnings.length ? h("div", { "data-testid": "wiz-warnings" }, p.warnings.map((w) => callout("warn", warnText(w)))) : null)),
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizSettings"))),
        h("div", { class: "card-body" },
          h("div", { class: "form-grid" }, field(t("slug"), f.slug), field(t("branch"), f.branch), field(t("buildMethod"), f.method),
            field(t("dockerfilePath"), f.dockerfile), field(t("port"), f.port, p.port_source === "EXPOSE" ? t("wizPortExpose") : t("wizPortDefault"))),
          h("details", {}, h("summary", {}, t("wizAdvanced")), h("div", { class: "form-grid" }, field(t("envName"), f.env), field(t("subPath"), f.subPath),
            h("label", { class: "row" }, f.autoDeploy, t("wizAutoDeploy")), h("label", { class: "row" }, f.approval, t("wizApproval")),
            h("label", { class: "row" }, f.manual, t("wizManualMode")))))),
      bpBox, tplCard,
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizWhere"))), h("div", { class: "card-body" }, targetBox)),
      (p.needs_services || []).length ? callout("warn", h("div", { "data-testid": "wiz-needs-services" }, h("strong", {}, t("wizNeedsServices")), " ",
        p.needs_services.map((s) => `${t(`svc_${s.service}`)} (${s.names.slice(0, 4).join(", ")})`).join("; ")), h("p", { class: "small" }, t("wizNeedsServicesHow"))) : null,
      p.env_hints.length ? h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizEnvHints"))),
        h("div", { class: "card-body" }, h("p", { class: "muted small" }, t("wizEnvHintsText")), h("div", { class: "chips", "data-testid": "wiz-env-hints" }, p.env_hints.map((n) => h("span", { class: "chip" }, n))))) : null,
      err,
      h("div", { class: "row" }, h("button", { class: "btn", type: "button", "data-testid": "wiz-back", onclick: () => stepUrl() }, t("wizBack")), h("span", { class: "spacer" }), create));
    syncMethod();
  }

  /* ───── шаг 3: результат ───── */
  function stepDone(r, sent) {
    clear(mount);
    const row = (kind, ...c) => h("div", { class: `callout ${kind}` }, ...c);
    const projectHref = `#/project/${enc(r.slug)}/${enc(r.environment)}`;
    const items = [row("ok", `✓ ${t("wizDoneProject", { slug: r.slug })}`)];
    if (r.target === "cluster") {
      if (r.provisioned) items.push(row("ok", `✓ ${t("wizDoneProvisioned", { ns: sent.namespace || r.slug })}`));
      else {
        const manifest = h("pre", { class: "manifest", "data-testid": "wiz-manifest" }, r.manifest_yaml || "");
        const deployNow = h("button", { class: "btn primary", type: "button", "data-testid": "wiz-deploy-now", onclick: async () => {
          deployNow.disabled = true;
          try { await api(`/api/projects/${enc(r.slug)}/redeploy?environment=${enc(r.environment)}${sent.head_sha ? `&revision=${sent.head_sha}` : ""}`, { method: "POST" }); toast(t("queued"), "ok"); deployNow.textContent = t("queued"); }
          catch (ex) { deployNow.disabled = false; toast(ex.message, "bad"); }
        } }, t("wizDeployNow"));
        items.push(row("warn", h("div", {}, `⚠ ${t(`wizManual_${r.reason}`)}`),
          h("p", { class: "small muted" }, t("wizManualHow")), manifest,
          h("div", { class: "row" }, h("button", { class: "btn", type: "button", "data-testid": "wiz-copy-manifest", onclick: () => copyText(r.manifest_yaml) }, t("copy")), deployNow)));
      }
    }
    const d = r.deploy || {};
    if (d.started) items.push(row("ok", `✓ ${t("wizDoneDeploy", { rev: String(d.revision || "").slice(0, 7) })}`));
    else if (d.reason === "approval_required") items.push(row("warn", `⚠ ${t("wizDeployApproval")}`));
    else if (d.reason === "deploy_not_requested") items.push(row("", t("wizDeployOff")));
    const wh = r.webhook || {};
    const whUrl = `${location.origin}${wh.path || ""}`;
    const secret = wh.secret ? h("div", { class: "secret-box mono", "data-testid": "wiz-webhook-secret" }, wh.secret) : null;
    const hookRes = h("div", { "data-testid": "wiz-hook-result" });
    const hookTok = h("input", { type: "password", autocomplete: "off", placeholder: t("wizHookToken"), "data-testid": "wiz-hook-token" });
    const hookBtn = h("button", { class: "btn", type: "button", "data-testid": "wiz-hook-create", onclick: async () => {
      if (!hookTok.value.trim()) { clear(hookRes); hookRes.append(callout("bad", t("wizHookNeedToken"))); return; }
      hookBtn.disabled = true; clear(hookRes);
      try {
        const x = await api(`/api/onboarding/${enc(r.slug)}/webhook`, { method: "POST", body: { token: hookTok.value.trim(), public_url: location.origin } });
        hookTok.value = "";
        if (x.ok) { hookRes.append(callout("ok", t(x.result === "updated" ? "wizHookUpdated" : "wizHookCreated"))); if (x.warning) hookRes.append(callout("warn", x.warning)); }
        else hookRes.append(callout("bad", x.error));
      } catch (ex) { hookRes.append(callout("bad", ex.message)); }
      hookBtn.disabled = false;
    } }, t("wizHookCreate"));
    const autoHook = h("div", { class: "stack", "data-testid": "wiz-hook-auto" }, h("div", { class: "small muted" }, t("wizHookHint")), field(t("wizHookToken"), hookTok), h("div", { class: "row" }, hookBtn), hookRes);
    const whCard = h("section", { class: "card", "data-testid": "wiz-webhook" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizWebhookTitle"))),
      h("div", { class: "card-body" }, autoHook, h("p", { class: "muted" }, t("wizWebhookText", { provider: wh.provider || "" })),
        field("Payload URL", h("div", { class: "secret-box mono", "data-testid": "wiz-webhook-url" }, whUrl)),
        secret ? field(t("wizWebhookSecret"), secret) : callout("", t("wizWebhookShared")),
        field(t("wizWebhookEvents"), h("div", { class: "chips" }, (wh.events || []).map((e) => h("span", { class: "chip" }, e)))),
        h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: () => copyText(whUrl) }, `${t("copy")} URL`), secret ? h("button", { class: "btn", type: "button", onclick: () => copyText(wh.secret) }, `${t("copy")} ${t("wizWebhookSecret")}`) : null)));
    mount.append(stepper(3), h("section", { class: "card", "data-testid": "wiz-result" }, h("div", { class: "card-head" }, h("span", { class: "card-title" }, t("wizDone"))), h("div", { class: "card-body" }, items)), whCard, envImportCard({ slug: r.slug, environment: r.environment }),
      h("div", { class: "row" }, h("a", { class: "btn primary", href: projectHref, "data-testid": "wiz-open-project" }, t("wizOpenProject")), h("button", { class: "btn", type: "button", "data-testid": "wiz-another", onclick: () => { st.url = ""; st.token = ""; stepUrl(); } }, t("wizAnother"))));
  }

  stepUrl();
}
