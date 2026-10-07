// Сценарии платформы по ролям и устройствам (см. docs/usecases.md, P2, P7, P8, P14). Дополняет ui.spec.js.
const { test, expect } = require("@playwright/test");

const ADMIN = "ui-test-admin-token-0123456789abcdef";
const H = (t) => ({ Authorization: `Bearer ${t}` });
const uniq = (p) => `${p}-${Math.random().toString(36).slice(2, 7)}`;

async function login(page, token) {
  await page.goto("/ui/");
  await page.getByTestId("use-token").click(); await page.getByTestId("token-input").fill(token); await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("nav-projects")).toBeVisible();
}
async function mkToken(request, role) {
  const r = await request.post("/api/tokens", { headers: H(ADMIN), data: { name: uniq(role), role } });
  return (await r.json()).token;
}
async function mkProject(request, slug) {
  await request.post("/api/projects", { headers: H(ADMIN), data: { slug, repo_full_name: `acme/${slug}`, environments: [{ name: "prod", namespace: "apps", deployment_name: slug, container_name: slug }] } });
}
async function noPageScroll(page, where) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw, `horizontal scroll at ${where}`).toBeLessThanOrEqual(cw + 1);
}
const isMobile = (info) => info.project.name === "mobile";
const PAGES = ["overview", "projects", "approvals", "explorer", "notifications", "audit", "users", "tokens", "license"];

test.describe("P14 all sections at phone and desktop width, light and dark", () => {
  for (const theme of ["light", "dark"]) {
    test(`sections without page-level horizontal scroll (${theme})`, async ({ page, request }, info) => {
      const slug = uniq("uc"); await mkProject(request, slug);
      await page.addInitScript((t) => { try { localStorage.setItem("dsp_theme", t); localStorage.setItem("dsp_lang", "en"); } catch (e) { /* ignore */ } }, theme);
      await login(page, ADMIN);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const bg = await page.evaluate(() => getComputedStyle(document.documentElement).backgroundColor);
      const lum = (c) => { const m = c.match(/\d+/g).map(Number); return (m[0] + m[1] + m[2]) / 3; };
      if (theme === "dark") expect(lum(bg)).toBeLessThan(60); else expect(lum(bg)).toBeGreaterThan(200);
      for (const name of PAGES) {
        await page.goto(`/ui/#/${name}`); await page.waitForTimeout(300);
        await expect(page.locator("main.page")).toBeVisible();
        await noPageScroll(page, `#/${name} ${info.project.name}`);
      }
      await page.goto(`/ui/#/project/${slug}/prod`); await page.waitForTimeout(400);
      await noPageScroll(page, `project ${info.project.name}`);
      await page.screenshot({ path: `/tmp/ui-shots/uc-${info.project.name}-${theme}-project.png` });
      await page.goto("/ui/#/new"); await page.waitForTimeout(300); await noPageScroll(page, `wizard ${info.project.name}`);
    });
  }
  test("navigation is reachable on a phone", async ({ page }, info) => {
    test.skip(!isMobile(info), "phone only");
    await login(page, ADMIN);
    const nav = page.locator("aside.side");
    await expect(nav).toBeVisible();
    for (const n of ["nav-projects", "nav-explorer", "nav-license"]) {                      // пункты доступны прокруткой полосы или видны сразу
      await page.getByTestId(n).scrollIntoViewIfNeeded(); await expect(page.getByTestId(n)).toBeVisible();
    }
  });
});

test("P2 theme and language are remembered", async ({ page }) => {
  await login(page, ADMIN);
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload(); await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByTestId("theme-toggle").click(); await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload(); await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByTestId("lang-menu").click(); await page.getByTestId("lang-ru").click();
  await expect(page.getByTestId("nav-projects")).toContainText(/Проекты/);
  await page.reload(); await expect(page.getByTestId("nav-projects")).toContainText(/Проекты/);
});

test.describe("P7 P8 roles see only what they may use", () => {
  test("developer: no administration, can open a project", async ({ page, request }) => {
    const slug = uniq("dev"); await mkProject(request, slug);
    await login(page, await mkToken(request, "developer"));
    for (const hidden of ["nav-users", "nav-tokens"]) await expect(page.getByTestId(hidden)).toHaveCount(0);
    await page.goto(`/ui/#/project/${slug}/prod`); await expect(page.locator("main.page")).toContainText(slug);
    await expect(page.getByTestId("delete-project")).toHaveCount(0);
  });
  test("viewer: read only, no deploy or rollback buttons", async ({ page, request }) => {
    const slug = uniq("view"); await mkProject(request, slug);
    await login(page, await mkToken(request, "viewer"));
    await page.goto(`/ui/#/project/${slug}/prod`); await expect(page.locator("main.page")).toContainText(slug);
    await expect(page.getByRole("button", { name: /rebuild and roll out|пересобрать/i })).toHaveCount(0);
    for (const hidden of ["nav-users", "nav-tokens", "nav-license"]) await expect(page.getByTestId(hidden)).toHaveCount(0);
  });
});


test.describe("first launch follows the device, later launches the user's choice", () => {
  test.use({ colorScheme: "dark", locale: "ru-RU" });
  test("P2b dark device and Russian locale start dark and in Russian; saved choice wins afterwards", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.locator("html")).toHaveAttribute("lang", "ru");
    await page.evaluate(() => { localStorage.setItem("dsp_theme", "light"); localStorage.setItem("dsp_lang", "en"); });
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.evaluate(() => localStorage.setItem("dsp_theme", "auto")); await page.reload();        // устаревшее значение не ломает: берётся тема устройства
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  });
});
