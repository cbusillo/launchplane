import { expect, test, setClockTime, type Page } from "./state";

const SNAPSHOT_TIME = "2026-01-10T12:00:00Z";

async function session(page: Page, admin = true) {
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "fixture-csrf", is_administrator: admin,
    identity: { provider: "github", login: "fixture", github_id: 42, name: "Admin", email: "fixture@example.invalid", organizations: [], teams: [], role: "admin" },
  } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [] } }));
}

function inventory() {
  const now = SNAPSHOT_TIME;
  return { status: "ok", trace_id: "credentials", inventory: {
    collection_status: "collected", last_attempt_at: now, received_at: now, stale: false, coverage: "partial",
    fresh_until: new Date(Date.parse(now) + 30_000).toISOString(), failure_reason: null as string | null,
    findings: [{ source: "lab-ssh", name: "authorized_keys@SHA256:unmapped-fingerprint", kind: "unknown_key_found" },
      { source: "lab-ssh", name: "named-key", kind: "rotation_due" }],
    snapshot: { generated_at: now, observations: [{ source: "lab-ssh", status: "partial", reason: "access_filtered", trace: "",
      records: [{ name: "authorized_keys@SHA256:unmapped-fingerprint", restrictions: ["restrict", "no-pty"], purpose: "Unknown", rotation_due: "Unknown" }] }], findings: [] },
  } };
}

test("credentials show drift and coverage and refresh to stale failure without writes", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await setClockTime(page, SNAPSHOT_TIME);
  await session(page);
  const errors: string[] = [];
  const methods: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  let failed = false;
  let denied = false;
  let unavailable = false;
  await page.route("**/v1/admin/credential-inventory", route => {
    methods.push(route.request().method());
    if (denied) return route.fulfill({ status: 403, json: { trace_id: "denied", error: { code: "authorization_denied", message: "Inventory access denied." } } });
    if (unavailable) return route.fulfill({ status: 503, json: { trace_id: "unavailable", error: { code: "unavailable", message: "Inventory is unreachable." } } });
    const response = inventory();
    if (failed) { response.inventory.collection_status = "failed"; response.inventory.failure_reason = "invalid_snapshot"; }
    return route.fulfill({ json: response });
  });
  await page.goto("/ui/engineering/credentials");
  await expect(page.getByRole("heading", { name: "Credentials", level: 1 })).toBeFocused();
  const summary = page.getByRole("status", { name: "Collection status" });
  await expect(summary).toHaveAttribute("data-result", "collected");
  await expect(summary).toHaveAttribute("data-coverage", "partial");
  await expect(summary).toHaveAttribute("data-stale", "false");
  await expect(page.getByRole("region", { name: "Drift and rotation" }).getByRole("listitem")).toHaveCount(inventory().inventory.findings.length);
  await expect(page.getByText("restrict, no-pty", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("credentials.png"), fullPage: true });
  failed = true;
  await page.clock.runFor(60_000);
  await expect(summary).toHaveAttribute("data-result", "failed");
  await expect(summary).toHaveAttribute("data-stale", "true");
  await expect(page.getByText("restrict, no-pty", { exact: true })).toBeVisible();
  unavailable = true;
  await page.getByRole("button", { name: "Refresh credentials" }).click();
  await expect(summary).toHaveAttribute("data-confirmed", "false");
  denied = true;
  await page.getByRole("button", { name: "Refresh credentials" }).click();
  await expect(page.getByRole("region", { name: "lab-ssh", exact: true })).toHaveCount(0);
  await expect(summary).toHaveCount(0);
  await expect(page.getByRole("alert")).toBeVisible();
  const deniedRequestCount = methods.length;
  await page.clock.runFor(60_000);
  expect(methods.length).toBe(deniedRequestCount);
  denied = false;
  unavailable = false;
  await page.getByRole("button", { name: "Refresh credentials" }).click();
  await expect(summary).toHaveAttribute("data-result", "failed");
  await expect(page.getByText("restrict, no-pty", { exact: true })).toBeVisible();
  expect(methods.every(method => method === "GET")).toBe(true);
  expect(errors).toEqual([]);
});

test("non-admin credentials route makes no inventory request", async ({ page }) => {
  await session(page, false);
  const requests: string[] = [];
  await page.route("**/v1/admin/credential-inventory", route => { requests.push(route.request().method()); return route.abort(); });
  await page.goto("/ui/engineering/credentials");
  await expect(page.getByRole("alert")).toBeVisible();
  expect(requests).toEqual([]);
});

test("empty credentials stay unknown", async ({ page }) => {
  await session(page);
  await page.route("**/v1/admin/credential-inventory", route => route.fulfill({ json: {
    status: "ok", trace_id: "empty", inventory: { collection_status: "never", last_attempt_at: null, received_at: null,
      stale: true, fresh_until: null, failure_reason: null, coverage: "unknown", snapshot: null, findings: [] },
  } }));
  await page.goto("/ui/engineering/credentials");
  const summary = page.getByRole("status", { name: "Collection status" });
  await expect(summary).toHaveAttribute("data-result", "never");
  await expect(summary).toHaveAttribute("data-coverage", "unknown");
  await expect(page.getByRole("table")).toHaveCount(0);
});
