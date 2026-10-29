import { defineConfig, devices } from "@playwright/test";
import { smokeArtifact } from "./browser-smoke-paths.mjs";
import { browserDeadlines } from "./playwright-deadlines.mjs";

export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "**/*.spec.ts",
  outputDir: smokeArtifact("test-results"),
  fullyParallel: true,
  workers: process.env.CI ? 4 : 2,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: browserDeadlines.test,
  reporter: "line",
  expect: {
    timeout: browserDeadlines.assertion,
  },
  use: {
    baseURL: process.env.BROWSER_SMOKE_URL,
    colorScheme: "dark",
    locale: "en-US",
    reducedMotion: "reduce",
    screenshot: "only-on-failure",
    timezoneId: "UTC",
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "narrow",
      grep: /@(?:layout|responsive)\b/,
      use: {
        ...devices["Desktop Chrome"],
        isMobile: false,
        viewport: { width: 390, height: 844 },
      },
    },
    {
      name: "mobile-safari",
      grep: /@webkit\b/,
      use: { ...devices["iPhone 13"] },
    },
  ],
  webServer: {
    command: "pnpm exec vite --mode browser-smoke --host 127.0.0.1 --port 0 --strictPort",
    env: { FORCE_COLOR: "0", NO_COLOR: "1" },
    // Each runner owns its OS-assigned listener; Playwright passes the captured
    // origin to worker processes before they load this configuration.
    wait: { stdout: /Local:\s+(?<browser_smoke_url>http:\/\/127\.0\.0\.1:\d+)\/ui\// },
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: browserDeadlines.assertion },
    timeout: browserDeadlines.server,
  },
});
