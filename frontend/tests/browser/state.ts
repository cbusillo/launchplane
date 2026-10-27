import { PROVIDER_HOLD_ATTENTION_ROUTE } from "../../src/api";

import { chromium, expect, test as base, type BrowserContext, type Page } from "@playwright/test";

// Freeze before the first navigation. Clock setup runs on an empty page, so
// advancing a day cannot dispatch app work. This leaves no short RPC window
// in which a busy host can run past pauseAt's target.
export const test = base.extend({
  context: async ({ context }, use) => {
    await freezeClock(context);
    // Existing journeys supply their own lane reads. Keep the independent
    // service read inert unless a page explicitly supplies attention evidence.
    await context.route(`**${PROVIDER_HOLD_ATTENTION_ROUTE}**`, route => route.fulfill({
      status: 503, json: { error: { code: "fixture_unavailable", message: "Service attention is not supplied by this journey." } },
    }));
    await use(context);
  },
});

export { expect };
export type { ConsoleMessage, Page, Route, TestInfo } from "@playwright/test";

// Persistent-profile tests create their own contexts and use the same setup.
async function freezeClock(context: BrowserContext) {
  const now = new Date();
  await context.clock.install({ time: new Date(0) });
  await context.clock.pauseAt(new Date(86_400_000));
  await context.clock.setSystemTime(now);
}

export async function launchControlledProfile(...args: Parameters<typeof chromium.launchPersistentContext>) {
  const context = await chromium.launchPersistentContext(...args);
  try {
    await freezeClock(context);
    return context;
  } catch (error) {
    await context.close();
    throw error;
  }
}

// Locator assertions already wait on rendered state. Use this for state held
// by route handlers or other asynchronous work outside the document instead.
// The suite's shared assertion deadline bounds failure, never successful work.
// Pass page only for artificial fixture delays. Timer/freshness behavior uses
// an explicit runFor followed by rendered-state assertions instead.
export function waitForState<T>(observe: () => T | Promise<T>, page?: Page) {
  return expect.poll(async () => {
    // Timer-delayed development fixtures settle only when the test drives
    // their clock. Advance virtual time, then check the actual state.
    if (page) await page.clock.runFor(1_000);
    return observe();
  });
}

// The clock stays paused across navigations and host stalls. Only a test's
// explicit runFor/fastForward advances timers; changing the date does not.
export async function setClockTime(page: Page, time: string | number | Date) {
  await page.clock.setSystemTime(new Date(time));
}
