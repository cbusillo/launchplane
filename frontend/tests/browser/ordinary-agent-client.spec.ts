import { expect, test } from "@playwright/test";

// This journey tests routing through a simulated login, not OAuth authority.
test("an exact operation link survives sign-in without an operation search", async ({ page }) => {
  let authenticated = false;
  const requested: string[] = [];
  const operationId = "privileged-operation-selected";
  const target = `/ui/engineering/privileged-operations?operation_id=${operationId}`;
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/v1/auth/session") {
      await route.fulfill({ status: authenticated ? 200 : 401, json: authenticated ? {
        status: "ok", csrf_token: "test-browser-csrf", identity: {
          provider: "github", login: "test-operator", github_id: 1001,
          name: "Test operator", email: "operator@example.invalid",
          organizations: [], teams: [], role: "admin",
        },
      } : { error: { code: "authentication_required", message: "Sign in" } } });
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (path === `/v1/privileged-operations/plans/${operationId}/review`) {
      await route.fulfill({ json: { status: "ok", trace_id: "test-exact-review", review: {
        schema_version: 1, operation_id: operationId, descriptor_id: "managed-secret-reencryption",
        descriptor_version: 1, operation_class: "managed_secret_reencryption",
        safety_class: "secret_backed", title: "Managed-secret re-encryption review",
        requested_by_kind: "github_human",
        lifecycle: { status: "planned", generated_at: "2026-09-09T10:00:00Z",
          created_at: "2026-09-09T10:00:00Z", updated_at: "2026-09-09T10:00:00Z",
          expires_at: "2026-09-09T11:00:00Z", expiry_state: "active", terminal_at: "",
          terminal_reason_available: false, approval_recorded: false, execution_recorded: false },
        blockers: { state: "clear", policy_safety_blocker_count: 0,
          operational_readiness_blocker_count: 0, unreadable_secret_count: 0, codes: [] },
        change: { summary: "The exact requested operation.", changed: true, metrics: [] },
        blast_radius: { scope: "managed_secret_store", summary: "Requested integration only.", affected_count: 1 },
        rollback: { rollback_class: "key_retained", summary: "Existing key remains retained." },
        evidence: { result_status: "ok", raw_detail_available: false, redaction: "semantic_only", digests: [] },
        activity: [], can_approve: false, can_revoke: false,
        authorizes_approval: false, authorizes_execution: false, persists_state: false,
      } } });
    } else {
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected request" } } });
    }
  });
  await page.route("**/auth/github/login?*", async (route) => {
    const destination = new URL(route.request().url()).searchParams.get("return_to");
    expect(destination).toBe(target);
    authenticated = true;
    await route.fulfill({ status: 302, headers: { location: destination! } });
  });
  await page.goto(target);
  await page.getByRole("link", { name: "Sign in with GitHub" }).click();
  await expect(page).toHaveURL(new RegExp(`${operationId}$`));
  await expect(page.getByRole("heading", { name: "Managed-secret re-encryption review" })).toBeVisible();
  await expect(page.getByRole("link", { name: "All operation plans" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh plans" })).toBeVisible();
  await page.getByRole("button", { name: "Refresh plans" }).click();
  await expect.poll(() => requested.filter((path) => path.endsWith(`/${operationId}/review`)).length).toBeGreaterThan(1);
  expect(requested).not.toContain("/v1/privileged-operations/plans");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({ path: `../tmp/browser-smoke/exact-operation-${test.info().project.name}.png`, fullPage: true });
});
