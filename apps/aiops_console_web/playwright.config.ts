import { defineConfig, devices } from "@playwright/test"


export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: {...devices["Desktop Chrome"], viewport: {width: 1440, height: 900}},
    },
    {
      name: "mobile-390-chromium",
      use: {...devices["Desktop Chrome"], viewport: {width: 390, height: 844}},
    },
  ],
  webServer: {
    command: "npm run dev -- --host 0.0.0.0 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
  },
})
