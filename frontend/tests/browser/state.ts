import { expect, test as base, type BrowserContext, type Page } from "@playwright/test";

// Freeze before the first navigation. Clock setup runs on an empty page, so
// advancing a day cannot dispatch app work. This leaves no short RPC window
// in which a busy host can run past pauseAt's target.
export const test = base.extend({
  context: async ({ context }, use) => {
    await freezeClock(context);
    await use(context);
  },
});

export { expect };
export { chromium } from "@playwright/test";
export type { ConsoleMessage, Page, Route, TestInfo } from "@playwright/test";

// Persistent-profile tests create their own contexts and use the same setup.
export async function freezeClock(context: BrowserContext) {
  const now = new Date();
  await context.clock.install({ time: new Date(0) });
  await context.clock.pauseAt(new Date(86_400_000));
  await context.clock.setSystemTime(now);
}

// Locator assertions already wait on rendered state. Use this for state held
// by route handlers or other asynchronous work outside the document instead.
// The suite's shared assertion deadline bounds failure, never successful work.
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
