const { chromium } = require("@playwright/test");
const ADMIN = "ui-test-admin-token-0123456789abcdef";
(async () => {
  const b = await chromium.launch();
  for (const theme of ["light", "dark"]) {
    const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, locale: "ru-RU" });
    await ctx.addInitScript((t) => { try { localStorage.setItem("dsp_theme", t); localStorage.setItem("dsp_lang", "ru"); } catch (e) {} }, theme);
    const p = await ctx.newPage();
    const errs = []; p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
    await p.goto("http://127.0.0.1:18098/ui/");
    await p.getByTestId("use-token").click(); await p.getByTestId("token-input").fill(ADMIN); await p.getByTestId("token-submit").click();
    await p.getByTestId("nav-projects").waitFor();
    for (const [name, hash] of [["overview", "#/overview"], ["projects", "#/projects"], ["project", "#/project/shop/prod"], ["audit", "#/audit"], ["explorer", "#/explorer"], ["license", "#/license"], ["users", "#/users"]]) {
      await p.evaluate((h) => (location.hash = h), hash); await p.waitForTimeout(900);
      await p.screenshot({ path: `/tmp/ui-shots/demo-${name}-${theme}.png` });
    }
    if (errs.length) console.log(theme, "ОШИБКИ:", errs.slice(0, 5));
    await ctx.close();
  }
  await b.close();
})();
