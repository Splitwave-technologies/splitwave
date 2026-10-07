const { chromium } = require("@playwright/test");
const ADMIN = "ui-test-admin-token-0123456789abcdef";
(async () => {
  const b = await chromium.launch();
  for (const theme of ["light", "dark"]) {
    const ctx = await b.newContext({ viewport: { width: 1180, height: 900 }, locale: "en-GB", deviceScaleFactor: 1 });
    await ctx.addInitScript((t) => { try { localStorage.setItem("dsp_theme", t); localStorage.setItem("dsp_lang", "en"); } catch (e) {} }, theme);
    const p = await ctx.newPage();
    await p.goto("http://127.0.0.1:18098/ui/");
    await p.getByTestId("use-token").click(); await p.getByTestId("token-input").fill(ADMIN); await p.getByTestId("token-submit").click();
    await p.getByTestId("nav-projects").waitFor();
    await p.evaluate(() => (location.hash = "#/project/shop/prod")); await p.waitForTimeout(1000);
    const card = (name) => p.locator(".card").filter({ has: p.locator("h2, h3, .card-h, .card-title").filter({ hasText: new RegExp("^" + name) }) }).first();
    for (const [n, name] of [[2, "Builds"], [3, "Releases"]]) {
      const c = card(name); await c.scrollIntoViewIfNeeded(); await c.screenshot({ path: `/tmp/ui-shots/stepc${n}-${theme}.png` });
    }
    await ctx.close();
  }
  await b.close();
})();
