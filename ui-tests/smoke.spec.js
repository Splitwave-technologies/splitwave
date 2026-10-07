// Проверка живого экземпляра (только чтение): UI_URL и UI_TOKEN задаются в окружении.
const { test, expect } = require("@playwright/test");
test.skip(!process.env.UI_TOKEN, "UI_TOKEN не задан");

test("live: overview, project runtime, builds and real build logs", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto("/ui/");
  await page.getByTestId("use-token").click();
  await page.getByTestId("token-input").fill(process.env.UI_TOKEN);
  await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("tile-projects")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("chart-deploys").locator("svg path").first()).toBeVisible();
  await page.screenshot({ path: "/tmp/ui-shots/live-overview.png", fullPage: true });

  await page.goto("/ui/#/project/control-plane/prod");
  await expect(page.getByTestId("replicas")).toContainText(/1 \/ 1/, { timeout: 15000 });
  await expect(page.locator("[data-testid^=pod-]").first()).toContainText("Running");
  const firstBuild = page.locator("[data-testid^=build-control-plane-build-]").first();
  await expect(firstBuild).toBeVisible({ timeout: 15000 });
  await firstBuild.getByRole("button", { name: /logs|логи/i }).click();
  await expect(page.getByTestId("logs-text")).toContainText(/===|prepare|analyze|export|Paketo/i, { timeout: 15000 });
  await page.screenshot({ path: "/tmp/ui-shots/live-logs.png" });
  expect(errors).toEqual([]);
});
