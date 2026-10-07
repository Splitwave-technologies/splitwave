// Платформа: ни один текст не выходит за свою кнопку, плашку, карточку или ячейку — на разных ширинах, языках и темах.
const { test, expect } = require("@playwright/test");
const fs = require("fs");
const findOverflow = require("./overflow.js");
const ADMIN = "ui-test-admin-token-0123456789abcdef";
const PAGES = ["overview", "projects", "approvals", "explorer", "notifications", "audit", "users", "tokens", "license", "new"];

test("platform text stays inside its box", async ({ page, request }, info) => {
  test.skip(info.project.name !== "desktop", "walks its own widths");
  test.setTimeout(240000);
  const slug = "ovf" + Math.random().toString(36).slice(2, 6);
  await request.post("/api/projects", { headers: { Authorization: `Bearer ${ADMIN}` }, data: { slug, repo_full_name: `acme/${slug}`, environments: [{ name: "prod", namespace: "apps", deployment_name: slug, container_name: slug }, { name: "staging", namespace: "apps-stg", deployment_name: slug, container_name: slug }] } });
  const problems = [];
  for (const lang of ["en", "ru", "kk"]) for (const theme of ["light", "dark"]) {
    await page.addInitScript(([l, t]) => { try { localStorage.setItem("dsp_lang", l); localStorage.setItem("dsp_theme", t); } catch (e) { /* ignore */ } }, [lang, theme]);
    for (const w of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width: w, height: 900 });
      await page.goto("/ui/"); await page.evaluate(([l, t]) => { localStorage.setItem("dsp_lang", l); localStorage.setItem("dsp_theme", t); }, [lang, theme]); await page.reload();
      if (await page.getByTestId("use-token").count()) { await page.getByTestId("use-token").click(); await page.getByTestId("token-input").fill(ADMIN); await page.getByTestId("token-submit").click(); }
      await page.getByTestId("nav-projects").waitFor();
      for (const name of [...PAGES, `project/${slug}/prod`]) {
        await page.goto(`/ui/#/${name}`); await page.waitForTimeout(250);
        for (const f of await findOverflow(page)) problems.push(`${lang}/${theme} ${w}px #/${name.replace(slug, "X")}: ${f.kind} "${f.text}" in ${f.tag}${f.box ? " (box " + f.box + " +" + f.over + ")" : ""}`);
      }
    }
  }
  const uniq = [...new Set(problems)];
  fs.writeFileSync("/tmp/ui-overflow.txt", uniq.join("\n"));
  expect(uniq, uniq.slice(0, 30).join("\n")).toEqual([]);
});
