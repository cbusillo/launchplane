import { defineConfig, devices } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { browserDeadlines } from "./playwright-deadlines.mjs";

const frontendRoot = dirname(fileURLToPath(import.meta.url));
const artifactRoot = resolve(frontendRoot, "../tmp/browser-smoke");

export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "**/*.spec.ts",
  outputDir: resolve(artifactRoot, "test-results"),
  // Tests mock their own API reads in fresh contexts. CI runners can share a
  // host, so keep browser fan-out bounded independently of its CPU count.
  fullyParallel: true,
  workers: process.env.CI ? 4 : undefined,
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
  ],
  webServer: {
    command: "pnpm exec vite --host 127.0.0.1 --port 0 --strictPort",
    env: { FORCE_COLOR: "0", NO_COLOR: "1" },
    // Each runner owns its OS-assigned listener; Playwright passes the captured
    // origin to worker processes before they load this configuration.
    wait: { stdout: /Local:\s+(?<browser_smoke_url>http:\/\/127\.0\.0\.1:\d+)\/ui\// },
    reuseExistingServer: false,
    timeout: browserDeadlines.server,
  },
});
