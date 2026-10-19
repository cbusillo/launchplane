import { expect, test, type Page } from "./state";

const operationId = "privileged-operation-abcdef0123456789abcdef0123456789";

async function openReview(page: Page, state = "available", readOnly = false) {
  await page.goto("/ui/?fixture=products");
  const identity = await page.evaluate(async () => (await import("/ui/src/dev-fixtures.ts")).fixtureIdentity);
  if (readOnly) identity.role = "read_only";
  const requests: string[] = [];
  await page.route("**/v1/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/auth/session") {
      await route.fulfill({ json: { status: "ok", identity } });
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (path === `/v1/privileged-operations/plans/${operationId}/review`) {
      await route.fulfill({ json: { status: "ok", trace_id: "fixture-review", review: {
        operation_id: operationId, descriptor_id: "managed-authz-policy-set", operation_class: "managed_authz_policy_set",
        title: "Managed authorization policy review", requested_by_kind: "github_human",
        lifecycle: { status: "planned", created_at: "2026-10-19T20:00:00Z", updated_at: "2026-10-19T20:00:00Z", approval_recorded: false, execution_recorded: false },
        blockers: { state: "clear" }, change: { summary: "Remove rules from the reviewed access policy.", metrics: [] },
        blast_radius: { summary: "The reviewed rules only." }, rollback: { summary: "Separate reviewed change." },
        evidence: { result_status: "ok", digests: [], raw_detail_available: true }, activity: [], can_approve: true, can_revoke: false,
        access_policy: { state, rules: [
          { managed_rule_id: "only-role", change: "removed", principal: "GitHub login fixture-alex", roles: ["read_only"], permissions: ["operations.read"], scope_summary: "Products: fixture-product. Contexts: fixture-testing. Targets: context/global only.", role_effects: [{ principal: "GitHub login fixture-alex", before: "read_only", after: null, state: "lost" }] },
          { managed_rule_id: "covered-role", change: "removed", principal: "GitHub login fixture-blair", roles: ["admin"], permissions: ["operations.read"], scope_summary: "Products: fixture-product. Contexts: all. Targets: context/global only.", role_effects: [{ principal: "GitHub login fixture-blair", before: "admin", after: "admin", state: "retained" }] },
          { managed_rule_id: "local-rule", change: "added", principal: "Local operator agent:fixture", roles: [], permissions: ["authz_policy_operation.read"], scope_summary: "Products: launchplane. Contexts: launchplane. Targets: context/global only.", role_effects: [] },
        ] },
        secret_value: "secret-value-must-not-render",
      } } });
    } else {
      requests.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected fixture request" } } });
    }
  });
  await page.goto(`/ui/engineering/privileged-operations?operation_id=${operationId}`);
  return requests;
}

test("access review names principals, entire role loss, overlap and local operator permissions", { tag: "@layout" }, async ({ page }, testInfo) => {
  const requests = await openReview(page);
  const review = page.getByRole("region", { name: "People and access changes" });
  await expect(review).toBeVisible();
  await expect(review).toContainText("fixture-alex");
  await expect(review.locator('[data-role-effect="lost"]')).toContainText("fixture-alex");
  await expect(review).toContainText("fixture-blair");
  await expect(review.locator('[data-role-effect="retained"]')).toContainText("fixture-blair");
  await expect(review).toContainText("Local operator agent:fixture");
  await expect(review).toContainText("authz_policy_operation.read");
  await expect(page.getByRole("button", { name: "Approve plan", exact: true })).toBeEnabled();
  await expect(page.locator("body")).not.toContainText("secret-value-must-not-render");
  await page.getByText("Technical details", { exact: true }).click();
  await expect(review).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("access-policy-review.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(requests).toEqual([]);
});

test("missing current policy evidence is visibly unknown", async ({ page }) => {
  await openReview(page, "unavailable");
  const review = page.getByRole("region", { name: "People and access changes" });
  await expect(review.locator('[data-access-review-state="unavailable"]')).toBeVisible();
  await expect(review).not.toContainText("fixture-alex");
});

test("read-only browser does not render principal display", async ({ page }) => {
  await openReview(page, "available", true);
  await expect(page.getByRole("heading", { name: "Managed authorization policy review", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "People and access changes" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("fixture-alex");
  await expect(page.locator("body")).not.toContainText("secret-value-must-not-render");
});
