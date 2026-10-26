import { expect, test, type Page } from "./state";

async function session(page: Page, admin = true) {
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "fixture-csrf",
    identity: { provider: "github", login: "fixture", github_id: 42, name: "Admin", email: "fixture@example.invalid", organizations: [], teams: [], role: admin ? "admin" : "read_only" },
  } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [] } }));
}

function inventory() {
  const now = new Date().toISOString();
  return { status: "ok", trace_id: "credentials", inventory: {
    collection_status: "collected", last_attempt_at: now, received_at: now, stale: false, coverage: "partial",
    findings: [{ source: "lab-ssh", name: "authorized_keys@SHA256:unmapped-fingerprint", kind: "unknown_key_found" },
      { source: "lab-ssh", name: "named-key", kind: "rotation_due" }],
    snapshot: { generated_at: now, observations: [{ source: "lab-ssh", status: "partial", reason: "access_filtered", trace: "",
      records: [{ name: "authorized_keys@SHA256:unmapped-fingerprint", restrictions: ["restrict", "no-pty"], purpose: "Unknown", rotation_due: "Unknown" }] }], findings: [] },
  } };
}

test("credentials show drift and coverage and refresh to stale failure without writes", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await session(page);
  const errors: string[] = [];
  const methods: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  let failed = false;
  let denied = false;
  await page.route("**/v1/admin/credential-inventory", route => {
    methods.push(route.request().method());
    if (denied) return route.fulfill({ status: 403, json: { trace_id: "denied", error: { code: "authorization_denied", message: "Inventory access denied." } } });
    const response = inventory();
    if (failed) { response.inventory.collection_status = "failed"; response.inventory.stale = true; }
    return route.fulfill({ json: response });
  });
  await page.goto("/ui/engineering/credentials");
  await expect(page.getByRole("heading", { name: "Credentials", level: 1 })).toBeFocused();
  await expect(page.getByText("Unknown key found", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Drift and rotation" }).getByText("Rotation due", { exact: true })).toBeVisible();
  await expect(page.getByText("restrict, no-pty", { exact: true })).toBeVisible();
  await expect(page.getByText("Unreturned credentials in inaccessible or partial sources remain unknown.")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("credentials.png"), fullPage: true });
  failed = true;
  await page.clock.runFor(60_000);
  await expect(page.getByRole("heading", { name: "Latest collection failed" })).toBeVisible();
  await expect(page.getByText(/No fresh snapshot within/)).toBeVisible();
  await expect(page.getByText("restrict, no-pty", { exact: true })).toBeVisible();
  denied = true;
  await page.getByRole("button", { name: "Refresh credentials" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Refresh failed" })).toContainText("Inventory access denied.");
  expect(methods.every(method => method === "GET")).toBe(true);
  expect(errors).toEqual([]);
});

test("non-admin credentials route makes no inventory request", async ({ page }) => {
  await session(page, false);
  const requests: string[] = [];
  await page.route("**/v1/admin/credential-inventory", route => { requests.push(route.request().method()); return route.abort(); });
  await page.goto("/ui/engineering/credentials");
  await expect(page.getByText("Sign in as an administrator to view credentials.")).toBeVisible();
  expect(requests).toEqual([]);
});

test("empty credentials stay unknown", async ({ page }) => {
  await session(page);
  await page.route("**/v1/admin/credential-inventory", route => route.fulfill({ json: {
    status: "ok", trace_id: "empty", inventory: { collection_status: "never", last_attempt_at: null, received_at: null,
      stale: true, coverage: "unknown", snapshot: null, findings: [] },
  } }));
  await page.goto("/ui/engineering/credentials");
  await expect(page.getByRole("heading", { name: "No collection received" })).toBeVisible();
  await expect(page.getByText("Inventory and credential presence are unknown until a collection arrives.")).toBeVisible();
});
