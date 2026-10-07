const { chromium } = require("@playwright/test");
const ADMIN = "ui-test-admin-token-0123456789abcdef";
(async () => {
  const b = await chromium.launch();
  for (const theme of ["light", "dark"]) {
    const ctx = await b.newContext({ viewport: { width: 1180, height: 700 }, locale: "en-GB" });
    await ctx.addInitScript((t) => { try { localStorage.setItem("dsp_theme", t); localStorage.setItem("dsp_lang", "en"); } catch (e) {} }, theme);
    const p = await ctx.newPage();
    await p.goto("http://127.0.0.1:18098/ui/");
    await p.getByTestId("use-token").click(); await p.getByTestId("token-input").fill(ADMIN); await p.getByTestId("token-submit").click();
    await p.getByTestId("nav-projects").waitFor();
    // 1. connect: wizard with a repository link
    await p.evaluate(() => (location.hash = "#/projects")); await p.waitForTimeout(500);
    await p.getByTestId("onboard").click(); await p.waitForTimeout(500);
    const inp = p.locator("input").filter({ hasNot: p.locator("[type=password]") }).first();
    await inp.fill("https://github.com/acme/shop"); await p.waitForTimeout(300);
    await p.screenshot({ path: `/tmp/ui-shots/step1-${theme}.png` });
    // 2. push: builds and releases of an environment
    await p.evaluate(() => (location.hash = "#/project/shop/prod")); await p.waitForTimeout(900);
    await p.evaluate(() => window.scrollTo(0, 420)); await p.waitForTimeout(300);
    await p.screenshot({ path: `/tmp/ui-shots/step2-${theme}.png` });
    // 3. roll back: the releases list with the rollback buttons
    await p.evaluate(() => { const h = [...document.querySelectorAll("h2,h3")].find((e) => /release/i.test(e.textContent)); if (h) h.scrollIntoView(); }); await p.waitForTimeout(400);
    await p.screenshot({ path: `/tmp/ui-shots/step3-${theme}.png` });
    await ctx.close();
  }
  await b.close();
})();
