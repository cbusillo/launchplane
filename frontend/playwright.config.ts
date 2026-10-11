import { defineConfig, devices } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = dirname(fileURLToPath(import.meta.url));
const artifactRoot = resolve(frontendRoot, "../tmp/browser-smoke");

export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "**/*.spec.ts",
  outputDir: resolve(artifactRoot, "test-results"),
  // Each test mocks its own API reads in a fresh browser context, so tests
  // share no state; CI runs one worker per runner core.
  fullyParallel: true,
  workers: process.env.CI ? "100%" : undefined,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: "line",
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: "http://127.0.0.1:4173",
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
    command: "pnpm exec vite --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173/ui/",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
