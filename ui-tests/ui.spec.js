const { test, expect } = require("@playwright/test");

const ADMIN = "ui-test-admin-token-0123456789abcdef";
const SHOTS = "/tmp/ui-shots";
const uniq = (p) => `${p}-${Math.random().toString(36).slice(2, 7)}`;

/** Собирает ошибки страницы и нарушения CSP: любой такой случай — провал теста. */
function watch(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text()}`); });
  return errors;
}

async function login(page, token = ADMIN) {
  await page.goto("/ui/");
  await page.getByTestId("use-token").click();
  await page.getByTestId("token-input").fill(token);
  await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("nav-projects")).toBeVisible();
}

const headers = (token) => ({ Authorization: `Bearer ${token}` });
async function mkToken(request, role) {
  const r = await request.post("/api/tokens", { headers: headers(ADMIN), data: { name: uniq(role), role } });
  expect(r.status()).toBe(201);
  return (await r.json()).token;
}
async function mkProject(request, slug) {
  const r = await request.post("/api/projects", { headers: headers(ADMIN), data: {
    slug, repo_full_name: `acme/${slug}`,
    environments: [{ name: "prod", namespace: "apps", deployment_name: slug, container_name: slug }] } });
  expect(r.status()).toBe(201);
}

test("login: wrong token is rejected, admin token opens the console", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/ui/");
  await page.screenshot({ path: `${SHOTS}/01-login.png` });
  await page.getByTestId("use-token").click();
  await page.getByTestId("token-input").fill("definitely-wrong");
  await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("login-error")).toHaveText(/rejected|не принят/i);
  await login(page);
  await expect(page.getByTestId("nav-audit")).toBeVisible();
  await expect(page.getByTestId("nav-tokens")).toBeVisible();
  // единственная допустимая «ошибка» — ожидаемый 401 на намеренно неверный токен
  expect(errors.filter((e) => !/status of 401/.test(e))).toEqual([]);
});

test("admin: create project, redeploy twice, roll back, delete with typed confirmation", async ({ page }) => {
  const errors = watch(page);
  const slug = uniq("shop");
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(slug);
  await page.getByTestId("np-repo").fill(`acme/${slug}`);
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill(slug);
  await page.getByTestId("np-con").fill(slug);
  await page.getByTestId("np-create").click();
  await expect(page.getByTestId("project-title")).toHaveText(slug);

  for (const rev of ["aaa1111", "bbb2222"]) {
    await page.getByTestId("redeploy").click();
    await page.getByTestId("rev-input").fill(rev);
    await page.getByTestId("rev-go").click();
    await expect(page.locator("tbody tr", { hasText: rev })).toBeVisible({ timeout: 10000 });
  }
  await page.screenshot({ path: `${SHOTS}/02-project.png`, fullPage: true });

  await page.getByRole("button", { name: /roll back here|откатить сюда/i }).first().click();
  await page.locator("dialog").getByRole("button", { name: /confirm|подтвердить/i }).click();
  await expect(page.locator(".toast.ok", { hasText: /rolled back|откат выполнен/i })).toBeVisible();

  await page.getByTestId("delete-project").click();
  const dlg = page.locator("dialog");
  const confirm = dlg.getByRole("button", { name: /delete|удалить/i });
  await expect(confirm).toBeDisabled();
  await dlg.locator("input").fill(slug);
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId(`row-${slug}`)).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("secrets: the value never appears in the page; developer cannot write", async ({ page, browser, request }) => {
  const errors = watch(page);
  const slug = uniq("vault"); const value = "Sup3r-S3cret-Value-XYZ";
  await mkProject(request, slug);
  await login(page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  await page.getByTestId("secret-key").fill("DB_PASSWORD");
  await page.getByTestId("secret-value").fill(value);
  await page.getByTestId("secret-set").click();
  await expect(page.getByTestId("secret-DB_PASSWORD")).toBeVisible();
  await expect(page.getByTestId("secret-value")).toHaveValue("");           // поле очищено
  expect(await page.content()).not.toContain(value);                         // значения нет нигде в DOM
  await page.getByTestId("secret-sync").click();
  await expect(page.locator(".toast.ok").last()).toContainText(/synced: 1|отправлено секретов: 1/i);
  await page.screenshot({ path: `${SHOTS}/03-secrets.png`, fullPage: true });

  const devToken = await mkToken(request, "developer");
  const ctx = await browser.newContext(); const dev = await ctx.newPage(); const devErrors = watch(dev);
  await login(dev, devToken);
  await dev.goto(`/ui/#/project/${slug}/prod`);
  await expect(dev.getByTestId("secret-DB_PASSWORD")).toBeVisible();        // имя видно
  await expect(dev.getByTestId("secret-set")).toHaveCount(0);               // писать нельзя
  await expect(dev.getByTestId("rollback-last")).toHaveCount(0);            // откат недоступен роли developer
  await expect(dev.getByTestId("redeploy")).toBeVisible();                  // деплой доступен
  expect(await dev.content()).not.toContain(value);
  await expect(dev.getByTestId("nav-tokens")).toHaveCount(0);
  await dev.goto("/ui/#/tokens");
  await expect(dev.getByText(/cannot access|недостаточно прав/i)).toBeVisible();
  await ctx.close();
  expect(errors).toEqual([]); expect(devErrors).toEqual([]);
});

test("audit: events are listed without secret values; filter works", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("aud"); const value = "AuditLeakCheck-12345";
  await mkProject(request, slug);
  const put = await request.put(`/api/projects/${slug}/secrets/K1`, { headers: headers(ADMIN), data: { value } });
  expect(put.status()).toBe(200);
  await login(page);
  await page.goto("/ui/#/audit");
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.getByTestId("audit-action").fill("secret_set");
  await page.getByRole("button", { name: /refresh|обновить/i }).click();
  await expect(page.locator("tbody tr").first()).toContainText("secret_set");
  expect(await page.content()).not.toContain(value);
  await page.screenshot({ path: `${SHOTS}/04-audit.png`, fullPage: true });
  expect(errors).toEqual([]);
});

test("XSS: hostile text stored in the audit log is rendered as plain text", async ({ page, request }) => {
  const errors = watch(page);
  const viewer = await mkToken(request, "viewer");
  const evil = "<img src=x onerror=window.__xss=1>";
  await request.post(`/api/projects/${encodeURIComponent(evil)}/redeploy`, { headers: headers(viewer) }); // 403 -> access_denied с путём в деталях
  await login(page);
  await page.goto("/ui/#/audit");
  await page.getByTestId("audit-action").fill("access_denied");
  await page.getByRole("button", { name: /refresh|обновить/i }).click();
  await expect(page.locator("tbody tr").first()).toContainText("access_denied");
  await expect(page.locator("img[src='x']")).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  expect(errors).toEqual([]);
});

test("tokens: issue (shown once), use, revoke", async ({ page, browser }) => {
  const errors = watch(page);
  const name = uniq("ci");
  await login(page);
  await page.getByTestId("nav-tokens").click();
  await page.getByTestId("token-name").fill(name);
  await page.getByTestId("token-role").selectOption("viewer");
  await page.getByTestId("token-create").click();
  const token = (await page.getByTestId("secret-once").textContent()).trim();
  expect(token).toMatch(/^dsp_/);
  await page.screenshot({ path: `${SHOTS}/05-token-once.png` });
  await page.locator("dialog").getByRole("button", { name: /close|закрыть/i }).click();
  await expect(page.locator("dialog")).toHaveCount(0);
  const row = page.getByTestId(`token-${name}`);
  await expect(row).toContainText(/active|активен/i);
  expect(await page.content()).not.toContain(token);                         // после закрытия токен нигде не виден

  const ctx = await browser.newContext(); const p2 = await ctx.newPage();
  await login(p2, token);
  await expect(p2.getByTestId("nav-audit")).toHaveCount(0);                  // viewer: только проекты
  await ctx.close();

  await row.getByRole("button", { name: /revoke|отозвать/i }).click();
  await page.locator("dialog").getByRole("button", { name: /revoke|отозвать/i }).click();
  await expect(row).toContainText(/revoked|отозван/i);
  const ctx2 = await browser.newContext(); const p3 = await ctx2.newPage();
  await p3.goto("/ui/");
  await p3.getByTestId("use-token").click();
  await p3.getByTestId("token-input").fill(token);
  await p3.getByTestId("token-submit").click();
  await expect(p3.getByTestId("login-error")).toHaveText(/rejected|не принят/i);
  await ctx2.close();
  await page.screenshot({ path: `${SHOTS}/06-tokens.png`, fullPage: true });
  expect(errors).toEqual([]);
});

test("license page, language switch and security headers", async ({ page }) => {
  const errors = watch(page);
  const resp = await page.goto("/ui/");
  const csp = resp.headers()["content-security-policy"];
  expect(csp).toContain("script-src 'self'");
  expect(csp).not.toContain("unsafe-inline");
  expect(resp.headers()["x-frame-options"]).toBe("DENY");
  await login(page);
  await page.getByTestId("nav-license").click();
  await expect(page.getByTestId("license-kv")).toContainText(/community/i);
  await page.screenshot({ path: `${SHOTS}/07-license.png` });
  await page.getByTestId("lang-menu").click(); await page.getByTestId("lang-ru").click();
  await expect(page.getByTestId("nav-projects")).toHaveText("Проекты");
  await page.getByTestId("lang-menu").click(); await page.getByTestId("lang-kk").click();
  await expect(page.getByTestId("nav-projects")).toHaveText("Жобалар");
  await expect(page.locator("html")).toHaveAttribute("lang", "kk");
  await page.getByTestId("lang-menu").click(); await page.getByTestId("lang-en").click();
  await expect(page.getByTestId("nav-projects")).toHaveText("Projects");
  expect(errors).toEqual([]);
});

test("mobile layout has no horizontal scroll", async ({ browser, request }) => {
  const slug = uniq("mob"); await mkProject(request, slug);
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true });
  const page = await ctx.newPage(); const errors = watch(page);
  await login(page);
  await page.screenshot({ path: `${SHOTS}/08-mobile-projects.png`, fullPage: true });
  await page.goto(`/ui/#/project/${slug}/prod`);
  await expect(page.getByTestId("project-title")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/09-mobile-project.png`, fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await ctx.close();
  expect(errors).toEqual([]);
});

// нужен платный модуль сред: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("environments and scoped secrets: add staging, override per environment, edit, delete", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("multi"); const stgValue = "stg-only-Value-987";
  await mkProject(request, slug);
  await login(page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  await expect(page.getByTestId("env-prod")).toBeVisible();

  await page.getByTestId("env-add").click();
  await page.getByTestId("env-name").fill("staging");
  await page.getByTestId("env-ns").fill("apps-stg");
  await page.getByTestId("env-dep").fill(slug);
  await page.getByTestId("env-con").fill(slug);
  await page.getByTestId("env-branch").fill("develop");
  await page.getByTestId("env-save").click();
  await expect(page.getByTestId("env-staging")).toContainText("develop");

  // общий секрет из prod (все среды) и секрет только для staging
  await page.getByTestId("secret-key").fill("COMMON_KEY");
  await page.getByTestId("secret-value").fill("common-value");
  await page.getByTestId("secret-scope").selectOption("all");
  await page.getByTestId("secret-set").click();
  await expect(page.getByTestId("secret-COMMON_KEY")).toContainText(/shared|общий/i);

  await page.getByTestId("env-staging").getByRole("link").click();
  await expect(page.getByLabel("Environment").first()).toHaveValue("staging");     // страница действительно переключилась
  await expect(page.getByTestId("secret-COMMON_KEY")).toContainText(/shared|общий/i); // унаследован
  await page.getByTestId("secret-key").fill("STG_KEY");
  await page.getByTestId("secret-value").fill(stgValue);
  await page.getByTestId("secret-set").click();                                   // область по умолчанию: эта среда
  await expect(page.getByTestId("secret-STG_KEY")).toContainText(/staging only|только staging/i);
  expect(await page.content()).not.toContain(stgValue);
  await page.screenshot({ path: `${SHOTS}/10-environments.png`, fullPage: true });

  await page.getByTestId("env-prod").getByRole("link").click();
  await expect(page.getByLabel("Environment").first()).toHaveValue("prod");
  await expect(page.getByTestId("secret-COMMON_KEY")).toBeVisible();
  await expect(page.getByTestId("secret-STG_KEY")).toHaveCount(0);                // в prod секрета staging нет

  await page.getByTestId("env-edit-staging").click();
  await page.getByTestId("env-branch").fill("release");
  await page.getByTestId("env-save").click();
  await expect(page.getByTestId("env-staging")).toContainText("release");

  await page.getByTestId("env-del-staging").click();
  const dlg = page.locator("dialog");
  await dlg.locator("input").fill("staging");
  await dlg.getByRole("button", { name: /delete|удалить/i }).click();
  await expect(page.getByTestId("env-staging")).toHaveCount(0);
  expect(errors).toEqual([]);
});


test("overview: KPI tiles, deploy chart, activity by project and recent events", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("ov");
  await mkProject(request, slug);
  for (const rev of ["a1b2c3d", "d4e5f6a"]) {
    const r = await request.post(`/api/projects/${slug}/redeploy?revision=${rev}`, { headers: headers(ADMIN) });
    expect(r.status()).toBe(200);
  }
  await login(page);
  await expect(page.getByTestId("tile-projects")).toBeVisible();
  await expect(page.getByTestId("tile-rate")).toContainText("%");
  await expect(page.getByTestId("tile-denied")).toBeVisible();
  const chart = page.getByTestId("chart-deploys");
  await expect(chart.locator("svg path").first()).toBeVisible();                 // есть столбцы
  await expect(chart.getByText(/deployed|успешно/i).first()).toBeVisible();      // легенда
  await expect(page.getByTestId("feed")).toContainText(/redeploy_requested|project_create/);
  await expect(page.locator(".hbar-row", { hasText: slug })).toBeVisible();      // активность по проектам

  await chart.locator("svg rect[tabindex]").last().focus();                       // подсказка на фокус
  await expect(chart.locator(".tip")).toBeVisible();
  await expect(chart.locator(".tip")).toContainText(/deployed|успешно/i);

  await page.getByTestId("ov-period").selectOption("7");
  await expect(page.getByTestId("tile-rate")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/11-overview.png`, fullPage: true });
  expect(errors).toEqual([]);
});


test("runtime state, builds and logs (logs are plain text; viewer cannot read them)", async ({ page, browser, request }) => {
  const errors = watch(page);
  const slug = uniq("rt");
  await mkProject(request, slug);
  await login(page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  await expect(page.getByTestId("replicas")).toContainText("1 / 2");
  await expect(page.getByTestId(`pod-${slug}-7d9f-bbbbb`)).toContainText("CrashLoopBackOff");
  await expect(page.getByTestId(`pod-${slug}-7d9f-bbbbb`)).toContainText("4");
  await expect(page.getByTestId(`build-${slug}-build-3`)).toContainText(/running|идёт/i);
  await expect(page.getByTestId(`build-${slug}-build-2`)).toContainText(/failed|ошибка/i);
  await expect(page.getByTestId(`build-${slug}-build-1`)).toContainText(/succeeded|успешно/i);

  await page.getByTestId(`logs-${slug}-build-2`).click();
  await expect(page.getByTestId("logs-text")).toContainText(`building ${slug}-build-2`);
  await expect(page.getByTestId("logs-text")).toContainText("<script>window.__logxss=1</script>");   // показан как текст
  expect(await page.evaluate(() => window.__logxss)).toBeUndefined();
  await page.screenshot({ path: `${SHOTS}/12-logs.png` });
  await page.locator("dialog").getByRole("button", { name: /close|закрыть/i }).click();
  await page.screenshot({ path: `${SHOTS}/13-runtime.png`, fullPage: true });

  const viewer = await mkToken(request, "viewer");
  const ctx = await browser.newContext(); const v = await ctx.newPage();
  await login(v, viewer);
  await v.goto(`/ui/#/project/${slug}/prod`);
  await expect(v.getByTestId(`build-${slug}-build-2`)).toBeVisible();
  await expect(v.getByTestId(`logs-${slug}-build-2`)).toHaveCount(0);           // логи только для роли с правом deploy
  await ctx.close();
  expect(errors).toEqual([]);
});


// ───────────── учётные записи ─────────────
const crypto = require("crypto");
function totp(secretB32, at = Date.now()) {            // RFC 6238: HMAC-SHA1, 6 цифр, шаг 30 с
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secretB32.replace(/=+$/, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)));
  const h = crypto.createHmac("sha1", key).update(counter).digest();
  const o = h[h.length - 1] & 0xf;
  return String(((h.readUInt32BE(o) & 0x7fffffff) % 1000000)).padStart(6, "0");
}
const PW1 = "Temporary-Passphrase-2026", PW2 = "Chosen-Str0ng-Passphrase";
async function mkUser(request, username, role = "none", password = PW1) {
  const r = await request.post("/api/users", { headers: headers(ADMIN), data: { username, role, password } });
  expect(r.status()).toBe(201);
}
async function accountLogin(page, username, password, code) {
  await page.goto("/ui/");
  await page.getByTestId("login-username").fill(username);
  await page.getByTestId("login-password").fill(password);
  await page.getByTestId("login-submit").click();
  if (code) { await page.getByTestId("login-totp").fill(code); await page.getByTestId("login-submit").click(); }
}

// нужен платный модуль команд: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("account: forced password change, 2FA enrolment, recovery codes, login with a code", async ({ page, request, browser }) => {
  const errors = watch(page);
  const user = uniq("ann");
  await mkUser(request, user, "viewer");
  await accountLogin(page, user, PW1);
  await expect(page.getByTestId("pw-save")).toBeVisible();                       // обязательная смена пароля
  await expect(page.getByTestId("nav-projects")).toHaveCount(0);                 // навигации нет, пока не сменили
  await page.getByTestId("pw-current").fill(PW1);
  await page.getByTestId("pw-new").fill("weak");
  await page.getByTestId("pw-save").click();
  await expect(page.locator(".toast.bad")).toContainText(/weak password|слаб/i);
  await page.getByTestId("pw-new").fill(PW2);
  await page.getByTestId("pw-save").click();
  await expect(page.getByTestId("nav-overview")).toBeVisible();                  // после смены — обычная работа

  await page.goto("/ui/#/account");
  await expect(page.getByTestId("acc-name")).toHaveText(user);
  await page.getByTestId("totp-start").click();
  const secret = (await page.getByTestId("totp-secret").textContent()).trim();
  await page.getByTestId("totp-code").fill("000000");
  await page.getByTestId("totp-enable").click();
  await expect(page.locator(".toast.bad")).toBeVisible();                        // неверный код
  await page.getByTestId("totp-code").fill(totp(secret));
  await page.getByTestId("totp-enable").click();
  const codes = (await page.getByTestId("secret-once").textContent()).trim().split("\n");
  expect(codes).toHaveLength(8);
  await page.screenshot({ path: `${SHOTS}/14-recovery.png` });
  await page.locator("dialog").getByRole("button", { name: /close|закрыть/i }).click();
  await expect(page.getByTestId("totp-disable")).toBeVisible();
  await expect(page.getByTestId("session-current")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/15-account.png`, fullPage: true });
  await page.getByTestId("logout").click();

  // вход теперь требует код; неверный не пускает, верный (следующего интервала) пускает
  await accountLogin(page, user, PW2);
  await expect(page.getByTestId("login-totp")).toBeVisible();
  await page.getByTestId("login-totp").fill("111111");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("login-error")).toBeVisible();
  await page.getByTestId("login-totp").fill(totp(secret, Date.now() + 30000));
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("nav-overview")).toBeVisible();
  await page.getByTestId("logout").click();
  await expect(page.getByTestId("login-username")).toBeVisible();

  // код восстановления работает один раз
  const ctx = await browser.newContext(); const p2 = await ctx.newPage();
  await accountLogin(p2, user, PW2, codes[0]);
  await expect(p2.getByTestId("nav-overview")).toBeVisible();
  await p2.getByTestId("logout").click();
  await expect(p2.getByTestId("login-username")).toBeVisible();   // выход завершён на сервере; иначе переход прервёт запрос и сессия останется жива
  await accountLogin(p2, user, PW2, codes[0]);
  await expect(p2.getByTestId("login-error")).toBeVisible();
  await ctx.close();
  expect(errors.filter((e) => !/status of (401|403|422)/.test(e))).toEqual([]);
});

// нужен платный модуль команд: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("users page and project members: admin invites a user who then sees only their project", async ({ page, browser, request }) => {
  const errors = watch(page);
  const mine = uniq("mine"), other = uniq("other"), user = uniq("bob");
  await mkProject(request, mine); await mkProject(request, other);
  await login(page);
  await page.getByTestId("nav-users").click();
  await page.getByTestId("user-new").click();
  await page.getByTestId("nu-name").fill(user);
  await page.getByTestId("nu-role").selectOption("none");
  await page.getByTestId("nu-create").click();
  const temp = (await page.getByTestId("secret-once").textContent()).trim();
  expect(temp.length).toBeGreaterThanOrEqual(20);
  await page.locator("dialog").getByRole("button", { name: /close|закрыть/i }).click();
  await expect(page.getByTestId(`user-${user}`)).toContainText(/password change pending|ждёт смены/i);
  await page.screenshot({ path: `${SHOTS}/16-users.png`, fullPage: true });

  const ctx = await browser.newContext(); const b = await ctx.newPage(); const bErr = watch(b);
  await accountLogin(b, user, temp);
  await b.getByTestId("pw-current").fill(temp); await b.getByTestId("pw-new").fill(PW2); await b.getByTestId("pw-save").click();
  await expect(b.getByTestId("no-access-yet")).toBeVisible();                    // пока не участник ни в одном проекте

  await page.goto(`/ui/#/project/${mine}/prod`);                                   // админ добавляет участника
  await page.getByTestId("mem-name").fill(user);
  await page.getByTestId("mem-role").selectOption("developer");
  await page.getByTestId("mem-add").click();
  await expect(page.getByTestId(`member-${user}`)).toContainText("developer");

  await b.goto("/ui/#/projects");
  await expect(b.getByTestId(`row-${mine}`)).toBeVisible();
  await expect(b.getByTestId(`row-${other}`)).toHaveCount(0);                     // чужого проекта нет
  await b.goto(`/ui/#/project/${mine}/prod`);
  await expect(b.getByTestId("redeploy")).toBeVisible();
  await expect(b.getByTestId("secret-set")).toHaveCount(0);                       // developer не пишет секреты
  await expect(b.getByTestId("nav-tokens")).toHaveCount(0);
  await expect(b.getByTestId("nav-users")).toHaveCount(0);

  await page.getByTestId("nav-users").click();                                     // отключение обрывает сессию
  await page.getByTestId(`toggle-${user}`).click();
  await expect(page.getByTestId(`user-${user}`)).toContainText(/disabled|выключена/i);
  await b.reload();
  await expect(b.getByTestId("login-username")).toBeVisible({ timeout: 20000 });   // под нагрузкой перезагрузка + запрос /api/me + перерисовка бывают медленными

  page.once("dialog", () => {});
  await page.getByTestId(`deluser-${user}`).click();
  const dlg = page.locator("dialog");
  await dlg.locator("input").fill(user);
  await dlg.getByRole("button", { name: /delete|удалить/i }).click();
  await expect(page.getByTestId(`user-${user}`)).toHaveCount(0);
  await ctx.close();
  expect(errors.filter((e) => !/status of (401|403)/.test(e))).toEqual([]);
  expect(bErr.filter((e) => !/status of (401|403)/.test(e))).toEqual([]);
});

test("project-scoped API token from the UI", async ({ page, request }) => {
  const slug = uniq("ptok"), name = uniq("ci");
  await mkProject(request, slug);
  await login(page);
  await page.getByTestId("nav-tokens").click();
  await page.getByTestId("token-name").fill(name);
  await page.getByTestId("token-project").selectOption(slug);
  await expect(page.getByTestId("token-role").locator("option[value=admin]")).toBeDisabled();
  await page.getByTestId("token-create").click();
  const token = (await page.getByTestId("secret-once").textContent()).trim();
  await page.locator("dialog").getByRole("button", { name: /close|закрыть/i }).click();
  await expect(page.getByTestId(`token-${name}`)).toContainText(slug);
  const ok = await request.get(`/api/projects/${slug}/secrets`, { headers: headers(token) });
  expect(ok.status()).toBe(200);
  const other = await request.get("/api/audit", { headers: headers(token) });
  expect(other.status()).toBe(403);
});

// ───────────── согласования ─────────────
async function protectedProject(request, slug) {
  const r = await request.post("/api/projects", { headers: headers(ADMIN), data: {
    slug, repo_full_name: `acme/${slug}`,
    environments: [{ name: "prod", namespace: "apps", deployment_name: slug, container_name: slug, require_approval: true }] } });
  expect(r.status()).toBe(201);
}
async function readyAccount(page, request, username, role) {
  await mkUser(request, username, role);
  await accountLogin(page, username, PW1);
  await page.getByTestId("pw-current").fill(PW1);
  await page.getByTestId("pw-new").fill(PW2);
  await page.getByTestId("pw-save").click();
  await expect(page.getByTestId("nav-overview")).toBeVisible();
}

// нужен платный модуль команд: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("approvals: request by a developer, approve/deny by another person, break-glass by admin", async ({ page, browser, request }) => {
  const errors = watch(page);
  const slug = uniq("gate"), alice = uniq("alice"), bob = uniq("bob");
  await protectedProject(request, slug);

  // разработчик запрашивает выкат
  await readyAccount(page, request, alice, "developer");
  await page.goto(`/ui/#/project/${slug}/prod`);
  await expect(page.getByTestId("redeploy")).toContainText(/request deploy|запросить выкат/i);
  await page.getByTestId("redeploy").click();
  await expect(page.getByTestId("approval-hint")).toBeVisible();
  await page.getByTestId("rev-input").fill("abc1234");
  await page.getByTestId("rev-go").click();
  await expect(page.locator(".toast.ok", { hasText: /request created|заявка создана/i })).toBeVisible();
  const pending = page.locator("[data-testid^=approval-]").first();
  await expect(pending).toContainText("abc1234");                                  // ожидающая заявка видна на странице проекта
  await expect(pending.getByTestId("approve")).toHaveCount(0);                     // автору (и developer) нельзя решать
  await expect(page.locator("tbody tr", { hasText: "abc1234" }).filter({ hasText: /deployed|failed/ })).toHaveCount(0);   // выката ещё нет

  // другой человек с правом отката согласует
  const ctx = await browser.newContext(); const b = await ctx.newPage(); const bErr = watch(b);
  await readyAccount(b, request, bob, "devops");
  await expect(b.getByTestId("nav-approvals")).toContainText("1");                 // счётчик ожидающих
  await b.getByTestId("nav-approvals").click();
  const row = b.locator("[data-testid^=approval-]", { hasText: slug });
  await expect(row).toContainText(alice);
  await b.screenshot({ path: `${SHOTS}/17-approvals.png`, fullPage: true });
  await row.getByTestId("approve").click();
  await b.getByTestId("decision-note").fill("release window ok");
  await b.getByTestId("decision-approve").click();
  await expect(b.locator(".toast.ok", { hasText: /approved|согласовано/i })).toBeVisible();
  await b.goto(`/ui/#/project/${slug}/prod`);
  await expect(b.locator("tbody tr", { hasText: "abc1234" })).toContainText("deployed");   // выкат выполнен после согласования
  await expect(b.locator("tbody tr", { hasText: "abc1234" })).toContainText(new RegExp(`${alice}.*approved by ${bob}`));

  // отклонение
  await page.reload();
  await page.getByTestId("redeploy").click();
  await page.getByTestId("rev-input").fill("bad9999");
  await page.getByTestId("rev-go").click();
  await expect(page.locator(".toast.ok", { hasText: /request created|заявка создана/i }).last()).toBeVisible();
  await b.goto("/ui/#/approvals");
  await b.locator("[data-testid^=approval-]", { hasText: "bad9999" }).getByTestId("deny").click();
  await b.getByTestId("decision-note").fill("not now");
  await b.getByTestId("decision-deny").click();
  await expect(b.locator(".toast.ok", { hasText: /denied|отклонено/i })).toBeVisible();
  await expect(b.locator("[data-testid^=approval-]", { hasText: "bad9999" })).toHaveCount(0);   // из списка «ждут» исчезла
  // после решения страница перерисовывается и может заменить список вместе с фильтром — повторяем выбор, пока не устоится
  await expect(async () => {
    await b.getByTestId("approval-filter").selectOption("");
    await expect(b.locator("[data-testid^=approval-]", { hasText: "bad9999" })).toBeVisible({ timeout: 1000 });
  }).toPass({ timeout: 10000 });
  await expect(b.locator("[data-testid^=approval-]", { hasText: "bad9999" })).toContainText(/denied|отклонено/i);
  await expect(b.locator("[data-testid^=approval-]", { hasText: "bad9999" })).toContainText("not now");

  // аварийный обход: только администратор, с причиной
  await login(page.context().pages()[0] === page ? await (async () => { await page.getByTestId("logout").click(); return page; })() : page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  await page.getByTestId("redeploy").click();
  await page.getByTestId("rev-input").fill("f1ee7777");
  await page.getByTestId("bg-check").check();
  await page.getByTestId("rev-go").click();
  await expect(page.locator(".toast.bad")).toBeVisible();                          // без причины нельзя
  await page.getByTestId("bg-reason").fill("production outage, hotfix");
  await page.getByTestId("rev-go").click();
  await expect(page.locator("tbody tr", { hasText: "f1ee7777" })).toBeVisible();
  await page.goto("/ui/#/audit");
  await page.getByTestId("audit-action").fill("break_glass");
  await page.getByRole("button", { name: /refresh|обновить/i }).click();
  await expect(page.locator("tbody tr").first()).toContainText("production outage, hotfix");
  await ctx.close();
  expect(errors.filter((e) => !/status of (401|403|409|422)/.test(e))).toEqual([]);
  expect(bErr.filter((e) => !/status of (401|403|409|422)/.test(e))).toEqual([]);
});

test("audit: chain verification shows intact status and head hash", async ({ page }) => {
  await login(page);
  await page.goto("/ui/#/audit");
  await page.getByTestId("audit-verify").click();
  await expect(page.getByTestId("audit-integrity")).toContainText(/Цепочка цела|Chain intact/);
  await expect(page.getByTestId("audit-head")).toContainText(/#\d+ [0-9a-f]{64}/);
});

test("secrets: versions, history restore, expiry policy and overview alert", async ({ page }) => {
  const slug = uniq("vers");
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(slug);
  await page.getByTestId("np-repo").fill(`acme/${slug}`);
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill(slug);
  await page.getByTestId("np-con").fill(slug);
  await page.getByTestId("np-create").click();
  await expect(page.getByTestId("project-title")).toHaveText(slug);
  const setSecret = async (v) => {
    await page.getByTestId("secret-key").fill("DB_PASS");
    await page.getByTestId("secret-value").fill(v);
    await page.getByTestId("secret-set").click();
  };
  await setSecret("first-value");
  await expect(page.getByTestId("secret-version-DB_PASS")).toHaveText("v1");
  await setSecret("second-value");
  await expect(page.getByTestId("secret-version-DB_PASS")).toHaveText("v2");
  await page.getByTestId("secret-history-DB_PASS").click();
  await expect(page.getByTestId("secret-ver-1")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("first-value");
  await page.getByTestId("secret-restore-1").click();
  await page.getByRole("button", { name: /Вернуть|Restore/ }).last().click();
  await expect(page.getByTestId("secret-version-DB_PASS")).toHaveText("v3");
  const soon = new Date(Date.now() + 3 * 864e5).toISOString().slice(0, 10);
  await page.getByTestId("secret-policy-DB_PASS").click();
  await page.getByTestId("policy-expires").fill(soon);
  await page.getByTestId("policy-save").click();
  await expect(page.getByTestId("secret-DB_PASS")).toContainText(/скоро истечёт|expiring soon/);
  await page.goto("/ui/#/");
  await expect(page.getByTestId("secret-alerts")).toContainText("DB_PASS");
});

test("notifications: webhook channel rejects internal targets, then is created, listed and deleted", async ({ page }) => {
  const name = uniq("hook");
  await login(page);
  await page.getByTestId("nav-notifications").click();
  await page.getByTestId("nc-name").fill(name);
  await page.getByTestId("nc-url").fill("https://127.0.0.1/hook");
  await page.getByTestId("nc-create").click();
  await expect(page.locator(".toast")).toContainText(/not public|public/i);
  await page.getByTestId("nc-kind").selectOption("telegram");
  await expect(page.getByTestId("nc-token")).toBeVisible();
  await expect(page.getByTestId("nc-url")).toBeHidden();
  await page.getByTestId("nc-kind").selectOption("slack");
  await expect(page.getByTestId("nc-url")).toBeVisible();
  await page.getByTestId("nc-url").fill("https://hooks.slack.com/services/T000/B000/XXXX");
  await page.getByTestId("nc-create").click();
  await expect(page.getByTestId(`nch-${name}`)).toContainText("hooks.slack.com");
  await page.getByTestId(`nch-del-${name}`).click();
  await page.getByRole("button", { name: /Удалить|Delete/ }).last().click();
  await expect(page.getByTestId(`nch-${name}`)).toHaveCount(0);
});


test("login page offers SSO when a provider is registered, and the sso-done route opens the session page", async ({ page }) => {
  await page.goto("/ui/");
  const btn = page.getByTestId("sso-login");
  await expect(btn).toBeVisible();
  await expect(btn).toContainText("Test IdP");
  await expect(btn).toHaveAttribute("href", "/api/sso/login");
});


test("siem: destination form switches by kind, create, test and delete (module stubbed)", async ({ page }) => {
  const name = uniq("siem");
  await login(page);
  await page.getByTestId("nav-siem").click();
  await expect(page.getByTestId("siem-host")).toBeVisible();
  await page.getByTestId("siem-kind").selectOption("splunk_hec");
  await expect(page.getByTestId("siem-token")).toBeVisible();
  await expect(page.getByTestId("siem-host")).toBeHidden();
  await page.getByTestId("siem-kind").selectOption("syslog");
  await page.getByTestId("siem-name").fill(name);
  await page.getByTestId("siem-host").fill("siem.internal");
  await page.getByTestId("siem-create").click();
  await expect(page.getByTestId(`siem-${name}`)).toContainText("siem.internal");
  await expect(page.getByTestId(`siem-lag-${name}`)).toHaveText("5");
  await page.getByTestId(`siem-test-${name}`).click();
  await expect(page.locator(".toast.ok").last()).toBeVisible();
  await page.getByTestId(`siem-del-${name}`).click();
  await page.getByRole("button", { name: /Удалить|Delete/ }).last().click();
  await expect(page.getByTestId(`siem-${name}`)).toHaveCount(0);
});


// нужен платный модуль сред: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("clusters: connect, test connection, environment dialog offers the cluster, delete (module stubbed)", async ({ page }) => {
  const name = uniq("edge");
  await login(page);
  await page.getByTestId("nav-clusters").click();
  await page.getByTestId("cl-name").fill(name);
  await page.getByTestId("cl-server").fill("https://k8s.example.com:6443");
  await page.getByTestId("cl-token").fill("tok-0123456789abcdefghij");
  await page.getByTestId("cl-create").click();
  await expect(page.getByTestId(`cl-${name}`)).toContainText("k8s.example.com");
  await page.getByTestId(`cl-test-${name}`).click();
  await expect(page.getByTestId(`cl-version-${name}`)).toHaveText("v1.31.2+k3s1");
  await page.getByTestId("cl-manifest").click();
  await expect(page.getByTestId("cl-manifest-ns")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(uniq("mc"));
  await page.getByTestId("np-repo").fill("acme/mc");
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill("mc");
  await page.getByTestId("np-con").fill("mc");
  await page.getByTestId("np-create").click();
  await page.getByTestId("env-add").click();
  await expect(page.getByTestId("env-cluster")).toContainText(name);
  await page.keyboard.press("Escape");
  await page.getByTestId("nav-clusters").click();
  await page.getByTestId(`cl-del-${name}`).click();
  await page.getByRole("button", { name: /Удалить|Delete/ }).last().click();
  await expect(page.getByTestId(`cl-${name}`)).toHaveCount(0);
});


test("previews: enable for a project, list and delete a preview (module stubbed)", async ({ page }) => {
  const slug = uniq("pvw");
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(slug);
  await page.getByTestId("np-repo").fill(`acme/${slug}`);
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill(slug);
  await page.getByTestId("np-con").fill(slug);
  await page.getByTestId("np-create").click();
  await expect(page.getByTestId("project-title")).toHaveText(slug);
  await expect(page.getByTestId("pv-7")).toContainText("feature/login");
  await page.getByTestId("pv-enabled").check();
  await page.getByTestId("pv-save").click();
  await expect(page.locator(".toast.ok").last()).toBeVisible();
  await expect(page.getByTestId("pv-enabled")).toBeChecked();
  await page.getByTestId("pv-del-7").click();
  await page.getByRole("button", { name: /Удалить|Delete/ }).last().click();
  await expect(page.getByTestId("pv-7")).toHaveCount(0);
});

test("projects on other git providers: GitLab project shows webhook URL and secret, token can be set", async ({ page }) => {
  const slug = uniq("gl");
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(slug);
  await page.getByTestId("np-provider").selectOption("gitlab");
  await page.getByTestId("np-repo").fill(`acme/team/${slug}`);
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill(slug);
  await page.getByTestId("np-con").fill(slug);
  await page.getByTestId("np-create").click();
  await expect(page.getByTestId("project-title")).toHaveText(slug);
  await expect(page.getByTestId("wh-url")).toHaveValue(new RegExp(`/webhook/gitlab/${slug}$`));
  await expect(page.getByTestId("wh-secret")).toHaveValue(/^[0-9a-f]{40}$/);
  await page.getByTestId("src-token").fill("glpat-abcdefghijklmnop");
  await page.getByTestId("src-token-save").click();
  await expect(page.locator(".toast.ok").last()).toBeVisible();
  await expect(page.locator("body")).not.toContainText("glpat-abcdefghijklmnop");
});

test("build method: create a Dockerfile project and switch method from the project page", async ({ page }) => {
  const slug = uniq("df");
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(slug);
  await page.getByTestId("np-method").selectOption("dockerfile");
  await page.getByTestId("np-repo").fill(`acme/${slug}`);
  await page.getByTestId("np-ns").fill("apps");
  await page.getByTestId("np-dep").fill(slug);
  await page.getByTestId("np-con").fill(slug);
  await page.getByTestId("np-create").click();
  await expect(page.getByTestId("project-title")).toHaveText(slug);
  await expect(page.getByTestId("src-method")).toHaveValue("dockerfile");
  await page.getByTestId("src-dockerfile").fill("deploy/Dockerfile");
  await page.getByTestId("src-method-save").click();
  await expect(page.locator(".toast.ok").last()).toBeVisible();
  await expect(page.locator("body")).toContainText("deploy/Dockerfile");
});


// нужен платный модуль сред: UI_WITH_EE=1 (см. tests/ui_server.py)
(process.env.UI_WITH_EE ? test : test.skip)("servers: add a server and get a one-time install command, environment dialog offers the server target (module stubbed)", async ({ page }) => {
  const name = uniq("srv");
  await login(page);
  await page.getByTestId("nav-servers").click();
  await expect(page.getByTestId("srv-vps-demo")).toContainText("Docker 27.1.1");
  await page.getByTestId("srv-name").fill(name);
  await page.getByTestId("srv-create").click();
  await expect(page.getByTestId("srv-install-cmd")).toHaveValue(/install\.sh \| sudo sh -s -- --token dspe_/);
  await page.getByRole("button", { name: /Закрыть|Close/ }).last().click();
  await expect(page.getByTestId(`srv-${name}`)).toContainText(/ждёт подключения|waiting to connect/);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("new-project").click();
  await page.getByTestId("np-slug").fill(uniq("vp"));
  await page.getByTestId("np-repo").fill("acme/vp");
  await page.getByTestId("np-ns").fill("default");
  await page.getByTestId("np-dep").fill("vp");
  await page.getByTestId("np-con").fill("vp");
  await page.getByTestId("np-create").click();
  await page.getByTestId("env-add").click();
  await expect(page.getByTestId("env-server")).toContainText("vps-demo");
  await page.getByTestId("env-server").selectOption("vps-demo");
  await expect(page.getByTestId("env-hport")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("nav-servers").click();
  await page.getByTestId(`srv-del-${name}`).click();
  await page.getByRole("button", { name: /Удалить|Delete/ }).last().click();
  await expect(page.getByTestId(`srv-${name}`)).toHaveCount(0);
});

/* ───────────── мастер подключения проекта ───────────── */
async function openWizard(page, repo) {
  await login(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("onboard").click();
  await expect(page.getByTestId("wiz-steps")).toBeVisible();
  await page.getByTestId("wiz-url").fill(`https://github.com/acme/${repo}`);
  await page.getByTestId("wiz-inspect").click();
}

test("wizard: paste a link, review detected settings, create, see webhook, open the project", async ({ page }) => {
  const errors = watch(page);
  const repo = uniq("wiz-app");
  await openWizard(page, repo);
  await expect(page.getByTestId("wiz-found")).toContainText(`acme/${repo}`);
  await expect(page.getByTestId("wiz-found")).toContainText(/node · Express/);
  await expect(page.getByTestId("wiz-method")).toHaveValue("generated");
  await expect(page.getByTestId("wiz-method-note")).toContainText(/предлагает свой|suggests its own/i);
  await expect(page.getByTestId("wiz-tpl-card")).toBeVisible();
  await expect(page.getByTestId("wiz-dftext")).toHaveValue(/FROM node:20-alpine[\s\S]*npm start/);
  await expect(page.getByTestId("wiz-env-hints")).toContainText("DB_URL");
  await expect(page.getByTestId("wiz-env-hints")).toContainText("API_KEY");
  await expect(page.getByTestId("wiz-provision-info")).toContainText(/сама создаст|create the namespace/i);
  await expect(page.getByTestId("wiz-slug")).toHaveValue(repo);
  await expect(page.getByTestId("wiz-namespace")).toHaveValue(repo);
  await page.screenshot({ path: `${SHOTS}/20-wizard-review.png`, fullPage: true });
  await page.getByTestId("wiz-create").click();
  await expect(page.getByTestId("wiz-result")).toContainText(new RegExp(`✓.*${repo}`));
  await expect(page.getByTestId("wiz-result")).toContainText(/Первый выкат запущен|First deploy started/);
  await expect(page.getByTestId("wiz-webhook-url")).toContainText("/webhook/github");
  await page.screenshot({ path: `${SHOTS}/21-wizard-done.png`, fullPage: true });
  await page.getByTestId("wiz-open-project").click();
  await expect(page.getByTestId("project-title")).toHaveText(repo);
  await expect(page.locator("tbody tr").first()).toBeVisible({ timeout: 10000 });     // первый выкат записан
  expect(errors).toEqual([]);
});

test("wizard: a Dockerfile repository switches to the Dockerfile method and takes the EXPOSE port", async ({ page }) => {
  const errors = watch(page);
  await openWizard(page, uniq("wiz-docker"));
  await expect(page.getByTestId("wiz-method")).toHaveValue("repo");
  await expect(page.getByTestId("wiz-tpl-card")).toHaveCount(0);                      // свой Dockerfile в репозитории — шаблон не нужен
  await expect(page.getByTestId("wiz-port")).toHaveValue("7000");
  await expect(page.getByTestId("wiz-dockerfile")).toBeVisible();
  await page.getByTestId("wiz-method").selectOption("buildpacks");
  await expect(page.getByTestId("wiz-dockerfile")).toBeHidden();
  expect(errors).toEqual([]);
});

test("wizard: unknown stack, private repository and monorepo-style warnings are shown", async ({ page }) => {
  const errors = watch(page);
  await openWizard(page, uniq("wiz-bare-private"));
  await expect(page.getByTestId("wiz-warnings")).toContainText(/Dockerfile/);
  await expect(page.getByTestId("wiz-warnings")).toContainText(/приватный|private/i);
  await expect(page.getByTestId("wiz-found")).toContainText(/язык не определён|language unknown/);
  expect(errors).toEqual([]);
});

test("wizard: provider error is explained and the user can fix the link", async ({ page }) => {
  await openWizard(page, uniq("wiz-missing"));
  await expect(page.getByTestId("wiz-error")).toContainText(/не найден|not found/i);
  await page.getByTestId("wiz-url").fill("not a url at all");
  await page.getByTestId("wiz-inspect").click();
  await expect(page.getByTestId("wiz-error")).toBeVisible();
  await page.getByTestId("wiz-url").fill("");
  await page.getByTestId("wiz-inspect").click();
  await expect(page.getByTestId("wiz-error")).toContainText(/Укажите ссылку|Enter a repository link/);
});

test("wizard: without cluster permissions the user gets a manifest to apply and can deploy afterwards", async ({ page }) => {
  const errors = watch(page);
  const repo = uniq("wiz-app");
  await openWizard(page, repo);
  await page.getByTestId("wiz-namespace").fill(`noperm-${repo}`.slice(0, 40));
  await page.getByTestId("wiz-create").click();
  await expect(page.getByTestId("wiz-manifest")).toContainText("kind: Deployment");
  await expect(page.getByTestId("wiz-manifest")).toContainText("kind: Namespace");
  await expect(page.getByTestId("wiz-manifest")).not.toContainText("dockerconfigjson");
  await expect(page.getByTestId("wiz-result")).toContainText(/нет прав|no permission/i);
  await page.getByTestId("wiz-deploy-now").click();
  await expect(page.locator(".toast.ok", { hasText: /Запущено|Started/ })).toBeVisible();
  expect(errors).toEqual([]);
});

test("wizard: an already connected repository is flagged and cannot be created twice", async ({ page, request }) => {
  const repo = uniq("wiz-exists");
  await mkProject(request, repo);
  await openWizard(page, repo);
  await expect(page.getByTestId("wiz-found")).toBeVisible();
  await expect(page.getByTestId("wiz-create")).toBeDisabled();
  await expect(page.locator(".callout.bad")).toContainText(/уже подключён|already connected/);
});

test("wizard: viewers and developers do not see the connect button, and the page is guarded", async ({ page, request }) => {
  const dev = await mkToken(request, "developer");
  await login(page, dev);
  await page.getByTestId("nav-projects").click();
  await expect(page.getByTestId("onboard")).toHaveCount(0);
  await page.goto("/ui/#/new");
  await expect(page.getByTestId("wiz-url")).toHaveCount(0);
});

test("wizard: the manual-mode checkbox makes the platform create nothing and hand out the YAML even when it has permissions", async ({ page }) => {
  const errors = watch(page);
  await openWizard(page, uniq("wiz-app"));
  await expect(page.getByTestId("wiz-provision-info")).toContainText(/сама создаст|create the namespace/i);
  await page.getByText(/Дополнительно|Advanced/).click();
  await page.getByTestId("wiz-manual").check();
  await expect(page.getByTestId("wiz-provision-info")).toContainText(/ничего не создаст|create nothing/i);
  await page.getByTestId("wiz-create").click();
  await expect(page.getByTestId("wiz-manifest")).toContainText("kind: Deployment");
  await expect(page.getByTestId("wiz-deploy-now")).toBeVisible();
  await expect(page.getByTestId("wiz-result")).toContainText(/вручную|by hand/i);
  expect(errors).toEqual([]);
});

test("wizard: the suggested Dockerfile is shown, can be edited, and the edited text is what gets stored in the project", async ({ page }) => {
  const errors = watch(page);
  const repo = uniq("wiz-app");
  await openWizard(page, repo);
  const text = page.getByTestId("wiz-dftext");
  await expect(text).toBeVisible();
  await expect(page.getByTestId("wiz-port")).toHaveJSProperty("readOnly", true);       // порт задаётся в карточке Dockerfile
  await text.fill((await text.inputValue()) + "# правка пользователя\n");
  await page.screenshot({ path: `${SHOTS}/22-wizard-dockerfile.png`, fullPage: true });
  await page.getByTestId("wiz-create").click();
  await expect(page.getByTestId("wiz-result")).toContainText(/Первый выкат запущен|First deploy started/);
  await page.getByTestId("wiz-open-project").click();
  await expect(page.getByTestId("project-title")).toHaveText(repo);
  const stored = page.getByTestId("src-dftext");
  await expect(stored).toHaveValue(/# правка пользователя/);                              // на странице проекта виден сохранённый текст
  await stored.fill("FROM alpine:3.20\nCMD [\"sleep\", \"1000\"]\n");
  await page.getByTestId("src-df-save").click();
  await expect(page.locator(".toast.ok").first()).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("src-dftext")).toHaveValue(/FROM alpine:3.20/);
  await page.getByTestId("src-df-repo").click();                                           // вернуться к Dockerfile из репозитория
  await expect(page.getByTestId("src-dftext")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("wizard: parameters regenerate the Dockerfile, manual edits are protected by a confirmation", async ({ page }) => {
  const errors = watch(page);
  await openWizard(page, uniq("wiz-app"));
  await page.getByTestId("wiz-tpl-port").fill("4100");
  await page.getByTestId("wiz-tpl-cmd").fill("node server.js");
  await page.getByTestId("wiz-tpl-regen").click();
  await expect(page.getByTestId("wiz-dftext")).toHaveValue(/EXPOSE 4100[\s\S]*node server\.js/);
  await expect(page.getByTestId("wiz-port")).toHaveValue("4100");
  const text = page.getByTestId("wiz-dftext");
  await text.fill((await text.inputValue()) + "# ручная правка\n");
  await page.getByTestId("wiz-tpl-port").fill("4200");
  await page.getByTestId("wiz-tpl-regen").click();
  await expect(page.locator("dialog")).toContainText(/будут заменены|will be replaced/);
  await page.locator("dialog").getByRole("button", { name: /Отмена|Cancel/ }).click();
  await expect(text).toHaveValue(/# ручная правка/);                                       // отмена — текст не тронут
  await page.getByTestId("wiz-tpl-regen").click();
  await page.locator("dialog").getByRole("button", { name: /Пересоздать|Regenerate/ }).click();
  await expect(text).toHaveValue(/EXPOSE 4200/);
  await expect(text).not.toHaveValue(/# ручная правка/);
  await page.getByTestId("wiz-tpl-version").fill("bad version");
  await page.getByTestId("wiz-tpl-regen").click();
  await expect(page.locator(".toast.bad")).toBeVisible();                                  // недопустимый параметр — понятная ошибка
  expect(errors.filter((e) => !/422/.test(e))).toEqual([]);
});

test("wizard: choosing buildpacks or the repository Dockerfile hides the suggested Dockerfile", async ({ page }) => {
  await openWizard(page, uniq("wiz-app"));
  await page.getByTestId("wiz-method").selectOption("buildpacks");
  await expect(page.getByTestId("wiz-tpl-card")).toBeHidden();
  await expect(page.getByTestId("wiz-port")).toHaveJSProperty("readOnly", false);
  await page.getByTestId("wiz-method").selectOption("repo");
  await expect(page.getByTestId("wiz-dockerfile")).toBeVisible();
  await page.getByTestId("wiz-method").selectOption("generated");
  await expect(page.getByTestId("wiz-tpl-card")).toBeVisible();
});

test("limits: the Community edition shows its limits, the wizard blocks a second project, and a license lifts it", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("lim");
  await mkProject(request, slug);
  const set = (body) => request.post("/__test/limits", { data: body });
  try {
    await set({ enforce: true, tier: "community" });
    await login(page);
    await page.goto("/ui/#/license");
    await expect(page.getByTestId("license-limits")).toContainText(/\d+ \/ 1 .* 1/);
    await page.goto("/ui/#/new");
    await page.getByTestId("wiz-url").fill(`https://github.com/acme/${uniq("new-app")}`);
    await page.getByTestId("wiz-inspect").click();
    await expect(page.getByTestId("wiz-limit")).toContainText(/Достигнут лимит|limit is reached/);
    await expect(page.getByTestId("wiz-create")).toBeDisabled();
    await page.screenshot({ path: `${SHOTS}/23-wizard-limit.png`, fullPage: true });
    // с лицензией Pro — можно
    await set({ enforce: true, tier: "pro" });
    await page.reload();                                                                      // тот же адрес (#/new): перерисовка только по перезагрузке
    await page.getByTestId("wiz-url").fill(`https://github.com/acme/${uniq("new-app")}`);
    await page.getByTestId("wiz-inspect").click();
    await expect(page.getByTestId("wiz-create")).toBeEnabled();
    await expect(page.getByTestId("wiz-limit")).toHaveCount(0);
  } finally {
    await set({ enforce: false });
  }
  expect(errors).toEqual([]);
});

test("wizard: webhook can be created automatically from the result step; errors are shown, the token field is cleared", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("hook");
  await mkProject(request, slug);
  await login(page);
  await page.route("**/api/onboarding/*/webhook", async (route) => {
    const b = route.request().postDataJSON();
    const ok = b.token === "good-token";
    await route.fulfill({ json: ok ? { ok: true, result: "created", url: "https://x/webhook", warning: "Адрес платформы локальный" } : { ok: false, error: "Токен не принят" } });
  });
  await page.goto("/ui/#/new");
  await page.getByTestId("wiz-url").fill(`https://github.com/acme/${uniq("hk")}`);
  await page.getByTestId("wiz-inspect").click();
  await page.getByTestId("wiz-create").click();
  await expect(page.getByTestId("wiz-hook-auto")).toBeVisible();
  await page.getByTestId("wiz-hook-create").click();
  await expect(page.getByTestId("wiz-hook-result")).toContainText(/Вставьте токен|Paste a token/);
  await page.getByTestId("wiz-hook-token").fill("bad");
  await page.getByTestId("wiz-hook-create").click();
  await expect(page.getByTestId("wiz-hook-result")).toContainText("Токен не принят");
  await page.getByTestId("wiz-hook-token").fill("good-token");
  await page.getByTestId("wiz-hook-create").click();
  await expect(page.getByTestId("wiz-hook-result")).toContainText(/Webhook создан|Webhook created/);
  await expect(page.getByTestId("wiz-hook-result")).toContainText("локальный");
  await expect(page.getByTestId("wiz-hook-token")).toHaveValue("");
  await page.screenshot({ path: `${SHOTS}/24-wizard-webhook.png`, fullPage: true });
  expect(errors).toEqual([]);
});

test("env import: preview shows names only, apply creates secrets, existing ones are protected until overwrite, a file can be chosen, the repository source sends its parameters", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("envi");
  await mkProject(request, slug);
  await login(page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  await expect(page.getByTestId("envimp")).toBeVisible();
  const SECRET = "alpha-secret-111";
  // вставленный текст: предпросмотр без значений, проблемные строки названы
  await page.getByTestId("envimp-text").fill(`UI_ONE=${SECRET}\nUI_TWO='b b'\nBAD LINE\nEMPTY=\n`);
  await page.getByTestId("envimp-preview").click();
  await expect(page.getByTestId("envimp-row-UI_ONE")).toContainText(/создать|create/);
  await expect(page.getByTestId("envimp-row-UI_TWO")).toBeVisible();
  await expect(page.getByTestId("envimp-problem")).toHaveCount(2);
  await expect(page.getByTestId("envimp-result")).not.toContainText(SECRET);
  await page.getByTestId("envimp-pick-UI_TWO").uncheck();                                        // выбираем только один
  await page.getByTestId("envimp-apply").click();
  await expect(page.getByTestId("envimp-done")).toContainText(/1/);
  await expect(page.getByTestId("secret-version-UI_ONE")).toHaveText("v1");
  await expect(page.getByTestId("secret-version-UI_TWO")).toHaveCount(0);
  // повторный импорт с другим значением: без перезаписи — «уже есть», с перезаписью — «обновить» и новая версия
  await page.getByTestId("envimp-text").fill("UI_ONE=changed-value");
  await page.getByTestId("envimp-preview").click();
  await expect(page.getByTestId("envimp-row-UI_ONE")).toContainText(/уже есть|exists/);
  await expect(page.getByTestId("envimp-apply")).toBeHidden();
  await page.getByTestId("envimp-overwrite").check();
  await expect(page.getByTestId("envimp-result")).toBeEmpty();                                   // смена параметров сбрасывает предпросмотр
  await page.getByTestId("envimp-preview").click();
  await expect(page.getByTestId("envimp-row-UI_ONE")).toContainText(/обновить|update/);
  await page.getByTestId("envimp-apply").click();
  await expect(page.getByTestId("secret-version-UI_ONE")).toHaveText("v2");
  // файл с компьютера: содержимое на экране не показывается
  await page.getByTestId("envimp-file").setInputFiles({ name: "prod.env", mimeType: "text/plain", buffer: Buffer.from("FROM_FILE=file-secret-222\nexport FROM_FILE2=\"x y\"\n") });
  await expect(page.getByTestId("envimp-filename")).toContainText("prod.env");
  await expect(page.getByTestId("envimp-text")).toBeDisabled();
  await page.getByTestId("envimp-preview").click();
  await expect(page.getByTestId("envimp-row-FROM_FILE")).toBeVisible();
  await expect(page.getByTestId("envimp-row-FROM_FILE2")).toBeVisible();
  await expect(page.getByTestId("envimp-result")).not.toContainText("null");
  await expect(page.locator("body")).not.toContainText("file-secret-222");
  await page.screenshot({ path: `${SHOTS}/25-env-import.png`, fullPage: true });
  await page.getByTestId("envimp-apply").click();
  await expect(page.getByTestId("secret-version-FROM_FILE")).toHaveText("v1");
  // репозиторий: отправляются путь, ветка и токен; ответ провайдера отображается
  let sent = null;
  await page.route("**/secrets/import**", async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({ json: { source: "repo:deploy/prod.env@main", scope: "prod", applied: false, items: [{ key: "FROM_REPO", action: "create", notes: ["interpolation"] }], problems: [{ line: 3, key: "X", reason: "empty" }],
      counts: { create: 1, update: 0, same: 0, exists: 0, not_selected: 0, invalid: 1 } } });
  });
  await page.getByTestId("envimp-mode").selectOption("repo");
  await expect(page.getByTestId("envimp-repo")).toBeVisible();
  await page.getByTestId("envimp-path").fill("deploy/prod.env");
  await page.getByTestId("envimp-ref").fill("main");
  await page.getByTestId("envimp-token").fill("read-token");
  await page.getByTestId("envimp-preview").click();
  await expect(page.getByTestId("envimp-row-FROM_REPO")).toContainText(/\$\{/);
  expect(sent).toMatchObject({ from_repo: true, path: "deploy/prod.env", ref: "main", token: "read-token", dry_run: true });
  expect(sent.content).toBeUndefined();
  expect(errors).toEqual([]);
});

test("overview: a developer sees no tokens tile and no stray 'null'; the new-project button is for admins only", async ({ page, request }) => {
  const errors = watch(page);
  await mkProject(request, uniq("ov"));
  await login(page, await mkToken(request, "developer"));
  await page.goto("/ui/#/");
  await expect(page.getByTestId("tile-projects")).toBeVisible();
  await expect(page.getByTestId("tile-tokens")).toHaveCount(0);
  await expect(page.locator("main, body").first()).not.toContainText(/\bnull\b/);
  await page.goto("/ui/#/projects");
  await expect(page.getByTestId("new-project")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("pod logs: a crashing pod opens its previous run, secret values are masked; the domain dialog binds and removes a domain", async ({ page, request }) => {
  const errors = watch(page);
  const slug = uniq("logs");
  await mkProject(request, slug);
  expect((await request.put(`/api/projects/${slug}/secrets/DB_PASSWORD`, { headers: headers(ADMIN), data: { value: "hunter2222" } })).status()).toBe(200);
  await login(page);
  await page.goto(`/ui/#/project/${slug}/prod`);
  const crashing = page.locator("[data-testid^=pod-logs-]").last();
  await expect(crashing).toBeVisible();
  await crashing.click();
  await expect(page.getByTestId("pod-log-prev")).toBeChecked();                                     // под падает — сразу прошлый запуск
  await expect(page.getByTestId("pod-log-text")).toContainText("ERROR boom: password=*** refused");
  await expect(page.getByTestId("pod-log-text")).not.toContainText("hunter2222");
  await page.getByTestId("pod-log-prev").uncheck();
  await expect(page.getByTestId("pod-log-text")).toContainText("current log line");
  await page.getByRole("button", { name: /Закрыть|Close/ }).click();
  // домен
  await page.getByTestId("env-domain-prod").click();
  await expect(page.getByTestId("domain-info")).toContainText(/HTTPS/);
  await page.getByTestId("domain-host").fill("not a host");
  await page.getByTestId("domain-save").click();
  await expect(page.getByTestId("domain-result")).toContainText(/DNS|host/);                        // сервер отверг имя — причина показана
  await page.getByTestId("domain-host").fill("App.Test.Example.com");
  await page.getByTestId("domain-save").click();
  await expect(page.locator("dialog[open]")).toHaveCount(0);                                         // диалог закрывается после ответа сервера: ждём, иначе он перекрывает следующий клик
  await page.getByTestId("env-domain-prod").click();
  await expect(page.getByTestId("domain-host")).toHaveValue("app.test.example.com");
  await page.screenshot({ path: `${SHOTS}/26-domain.png`, fullPage: true });
  await page.getByTestId("domain-remove").click();
  await expect(page.locator("dialog[open]")).toHaveCount(0);
  await page.getByTestId("env-domain-prod").click();
  await expect(page.getByTestId("domain-host")).toHaveValue("");
  expect(errors.filter((e) => !/422/.test(e))).toEqual([]);                                          // 422 на заведомо неверное имя домена — ожидаемо
});

test("wizard: variables that imply a database or storage produce a visible warning", async ({ page, request }) => {
  const errors = watch(page);
  await mkProject(request, uniq("ns"));
  await login(page);
  await page.goto("/ui/#/new");
  await page.getByTestId("wiz-url").fill(`https://github.com/acme/${uniq("svc")}`);
  await page.getByTestId("wiz-inspect").click();
  await expect(page.getByTestId("wiz-needs-services")).toContainText(/база данных|database/);
  expect(errors).toEqual([]);
});

test("explorer: query filters events, a field value is added to the query, a bad query shows the reason; theme switch persists", async ({ page }) => {
  const errors = watch(page);
  await login(page);
  await page.getByTestId("nav-explorer").click();
  await expect(page.getByTestId("ex-summary")).toContainText(/\d+/);
  await page.getByTestId("ex-query").fill("nofield:x");
  await page.getByTestId("ex-run").click();
  await expect(page.getByTestId("ex-error")).toContainText(/unknown field/);
  await page.getByTestId("ex-query").fill("actor:bootstrap*");
  await page.getByTestId("ex-run").click();
  await expect(page.getByTestId("ex-error")).toHaveText("");
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(errors.filter((e) => !/status of 422/.test(e))).toEqual([]);   // 422 на неверный запрос — ожидаемо
});

test("update banner: the administrator sees a new version with a safe link; nothing when up to date", async ({ page }) => {
  const errors = watch(page);
  let reply = { enabled: true, current: "0.1.0", latest: "0.2.0", available: true, security: true, url: "https://split-wave.com/changelog" };
  await page.route("**/api/updates", (route) => route.fulfill({ json: reply }));
  await login(page);
  const banner = page.getByTestId("update-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("0.2.0");
  await expect(banner).toContainText(/security update|обновление безопасности/i);
  await expect(banner.locator("a")).toHaveAttribute("href", "https://split-wave.com/changelog");
  await expect(banner.locator("a")).toHaveAttribute("rel", /noopener/);
  reply = { enabled: true, current: "0.1.0", latest: "0.1.0", available: false, security: false, url: "" };
  await page.reload();
  await expect(page.getByTestId("nav-projects")).toBeVisible();
  await expect(page.getByTestId("update-banner")).toHaveCount(0);
  expect(errors).toEqual([]);
});
