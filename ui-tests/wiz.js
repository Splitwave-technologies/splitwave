const { chromium } = require("@playwright/test");
const ADMIN = "ui-test-admin-token-0123456789abcdef";
(async () => {
  const b = await chromium.launch(); const ctx = await b.newContext({ viewport: { width: 1180, height: 820 }, locale: "en-GB" });
  await ctx.addInitScript(() => { try { localStorage.setItem("dsp_theme", "light"); localStorage.setItem("dsp_lang", "en"); } catch (e) {} });
  const p = await ctx.newPage();
  await p.goto("http://127.0.0.1:18098/ui/");
  await p.getByTestId("use-token").click(); await p.getByTestId("token-input").fill(ADMIN); await p.getByTestId("token-submit").click();
  await p.getByTestId("nav-projects").waitFor();
  await p.evaluate(() => (location.hash = "#/projects")); await p.waitForTimeout(500);
  await p.getByTestId("onboard").click(); await p.waitForTimeout(400);
  await p.locator("input").first().fill("https://github.com/heroku/python-getting-started");
  await p.getByRole("button", { name: /Check repository/ }).click(); await p.waitForTimeout(6000);
  await p.screenshot({ path: "/tmp/ui-shots/wiz-2.png", fullPage: true });
  await b.close();
})();
