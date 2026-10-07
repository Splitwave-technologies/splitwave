const { defineConfig } = require("@playwright/test");
module.exports = defineConfig({
  testDir: ".",
  testMatch: "*.spec.js",
  timeout: 30000,
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: { baseURL: process.env.UI_URL || "http://127.0.0.1:18099", headless: true, locale: "en-US" },
  projects: [
    { name: "desktop", testIgnore: [], use: { viewport: { width: 1280, height: 800 } } },
    { name: "mobile", testMatch: "usecases.spec.js", use: { viewport: { width: 390, height: 780 } } },
  ],
});
