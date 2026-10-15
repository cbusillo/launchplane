import assert from "node:assert/strict";
import { test } from "node:test";
import { checkBrowserTiming } from "../scripts/check-browser-timing.mjs";

const prefix = 'import { test, expect } from "./state";\n';
const check = source => checkBrowserTiming(prefix + source, "tests/browser/example.spec.ts");

test("browser timing gate rejects sleeps, aliases and test-specific failure budgets", () => {
  for (const source of [
    "await page.waitForTimeout(500)",
    "await page['waitForTimeout'](500)",
    "await new Promise(resolve => setTimeout(resolve, 500))",
    'import { setTimeout as pause } from "node:timers/promises"; await pause(500)',
    "const pause = globalThis.setTimeout; pause(done, 500)",
    "const { setTimeout: pause } = window; pause(done, 500)",
    "test.setTimeout(500)",
    "test.slow()",
    "page.setDefaultTimeout(500)",
    "context.setDefaultNavigationTimeout(500)",
    "await expect(row).toBeVisible({ timeout: 500 })",
    "await page.waitForFunction(ready, null, { timeout: 500 })",
    "await page.clock.install()",
    "await page.clock.pauseAt(new Date())",
    "await page['clock']['pauseAt'](new Date())",
    "await page.clock.resume()",
  ]) assert.ok(check(source).length > 0, source);
});

test("browser timing gate accepts rendered state, request gates and controlled time", () => {
  assert.deepEqual(check(`
    import { waitForState, setClockTime } from "./state";
    await expect(page.getByRole("heading")).toBeVisible();
    await waitForState(() => requests.length).toBe(1);
    await setClockTime(page, deadline);
    await page.clock.runFor(REFRESH_INTERVAL_MS);
    await page.waitForResponse(response => response.status() === 200);
    await new Promise(resolve => { release = resolve; });
    // setTimeout and { timeout: 500 } in comments or strings are not calls.
    const label = "sleep(500)";
  `), []);
});

test("browser timing gate prevents bypassing the shared page fixture", () => {
  assert.ok(checkBrowserTiming('import { test } from "@playwright/test";', "tests/browser/new.spec.ts").length);
  assert.ok(checkBrowserTiming('import { test } from "./another-helper";', "tests/browser/new.spec.ts").length);
  assert.deepEqual(check('import type { Page } from "@playwright/test";'), []);
});
