import { expect, test } from "@playwright/test";

// Production API adapter against local HTTP fixtures; all requests must be reads.
test("readable initial tab, refresh, explicit denial, and recovery", async ({ page }, testInfo) => {
  const calls: string[] = [];
  const mutations: string[] = [];
  const unexpected: string[] = [];
  page.on("request", request => {
    if (request.method() !== "GET") mutations.push(`${request.method()} ${request.url()}`);
  });
  await page.goto("/ui/?fixture=products");
  const identity = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return fixtures.fixtureIdentity;
  });
  await page.route("**/v1/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/v1/auth/session") {
      await route.fulfill({ json: { status: "ok", identity, csrf_token: "unused" } });
    } else if (url.pathname === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (url.pathname === "/v1/privileged-operations/plans") {
      const descriptor = url.searchParams.get("descriptor_id") ?? "managed-secret-reencryption";
      calls.push(descriptor);
      if (descriptor === "managed-merge-train-policy-import") {
        await route.fulfill({ json: { status: "ok", trace_id: "readable-plans", total: 0, reviews: [] } });
      } else {
        await route.fulfill({ status: 403, json: { trace_id: "denied-secret", error: { code: "authorization_denied", message: "Identity cannot access privileged-operation planning." } } });
      }
    } else if (url.pathname === "/v1/privileged-operations/merge-train-targets/inputs") {
      await route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: "No preparation access." } } });
    } else {
      unexpected.push(url.pathname);
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected test read" } } });
    }
  });
  await page.goto("/ui/engineering/privileged-operations");
  const mergeTab = page.getByRole("button", { name: "Merge-train policy", exact: true });
  await expect(mergeTab).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No changes are waiting for review")).toBeVisible();
  expect([...new Set(calls)]).toEqual(["managed-secret-reencryption", "managed-authz-policy-set", "managed-merge-train-policy-import"]);
  const beforeActiveClick = calls.length;
  await mergeTab.click();
  await expect(mergeTab).toHaveAttribute("aria-pressed", "true");
  expect(calls.length).toBe(beforeActiveClick);
  const beforeRefresh = calls.length;
  await page.getByRole("button", { name: "Refresh plans", exact: true }).click();
  await expect.poll(() => calls.length).toBeGreaterThan(beforeRefresh);
  await expect(page.getByText("No changes are waiting for review")).toBeVisible();
  expect(calls.slice(beforeRefresh)).toEqual(["managed-merge-train-policy-import"]);
  await page.getByRole("button", { name: "Secret rotation", exact: true }).click();
  await expect(page.getByText("You do not have access to secret rotation plans.")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("explicit-secret-denial.png"), fullPage: true });
  await mergeTab.click();
  await expect(mergeTab).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No changes are waiting for review")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("readable-merge-policy.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const beforeLink = calls.length;
  await page.goto("/ui/engineering/privileged-operations?descriptor_id=managed-secret-reencryption");
  await expect(page.getByText("You do not have access to secret rotation plans.")).toBeVisible();
  expect(new Set(calls.slice(beforeLink))).toEqual(new Set(["managed-secret-reencryption"]));
  await page.route("**/v1/privileged-operations/plans*", route => route.fulfill({
    status: 403,
    json: { error: { code: "authorization_denied", message: "No read access." } },
  }));
  await page.goto("/ui/engineering/privileged-operations");
  await expect(page.getByText("No readable plan types", { exact: true })).toBeVisible();
  await expect(mergeTab).toHaveAttribute("aria-pressed", "false");
  expect(mutations).toEqual([]);
  expect(unexpected).toEqual([]);
});


test("agent proposal approval waits for matching visible policy details", async ({ page }, testInfo) => {
  const operationId = "privileged-operation-abcdef0123456789abcdef0123456789";
  await page.goto("/ui/?fixture=products");
  const identity = await page.evaluate(async () => (await import("/ui/src/dev-fixtures.ts")).fixtureIdentity);
  const review = {
    schema_version: 1, operation_id: operationId,
    descriptor_id: "managed-authz-policy-set", descriptor_version: 1,
    operation_class: "managed_authz_policy_set", safety_class: "secret_free",
    title: "Review agent policy proposal", requested_by_kind: "local_operator",
    lifecycle: { status: "planned", created_at: "2026-10-05T12:00:00Z", updated_at: "2026-10-05T12:00:00Z", expires_at: "2026-10-06T12:00:00Z", expiry_state: "active" },
    blockers: { state: "clear" },
    change: { summary: "An agent prepared an authorization policy change.", changed: true, metrics: [] },
    blast_radius: { summary: "Only the proposed policy set.", scope: "managed_authz_policy" },
    rollback: { summary: "Prepare a separate reversal.", rollback_class: "forward_only" },
    evidence: { result_status: "ok", digests: [], raw_detail_available: true },
    activity: [], can_approve: true, can_revoke: false,
  };
  let detailMode: "error" | "mismatch" | "ready" = "error";
  const mutations: string[] = [];
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") mutations.push(path);
    if (path === "/v1/auth/session") {
      await route.fulfill({ json: { status: "ok", identity, csrf_token: "fixture-review-csrf" } });
    } else if (path === `/v1/privileged-operations/plans/${operationId}/review`) {
      await route.fulfill({ json: { status: "ok", trace_id: "review-fixture", review } });
    } else if (path === `/v1/privileged-operations/plans/${operationId}`) {
      if (detailMode === "error") {
        await route.fulfill({ status: 503, json: { error: { message: "Policy details are unavailable." } } });
      } else {
        await route.fulfill({ json: { status: "ok", record: {
          operation_id: detailMode === "mismatch" ? "another-plan" : operationId,
          requested_by: { identity_type: "local_operator", principal_sha256: "a".repeat(64) },
          request: { managed_set_id: "example.proposed-access", desired_policy: { local_operator_rules: [{ subjects: ["example-policy-agent"], token_labels: ["example-policy-token"], actions: ["example.read"], products: ["example-product"] }] } },
          evidence: { diff: { rules_added: 1, rules_removed: 0 } },
        }, events: [] } });
      }
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else {
      await route.fulfill({ status: 404, json: { error: { message: "No fixture for this request." } } });
    }
  });
  await page.goto(`/ui/engineering/privileged-operations?operation_id=${operationId}`);
  const details = page.getByRole("region", { name: "Proposed policy changes", exact: true });
  const approve = page.getByRole("button", { name: "Approve plan", exact: true });
  await expect(details.getByText("Policy details are unavailable.")).toBeVisible();
  await expect(approve).toBeDisabled();
  detailMode = "mismatch";
  await details.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(details.getByText("The proposal details do not match this plan.")).toBeVisible();
  await expect(approve).toBeDisabled();
  detailMode = "ready";
  await page.reload();
  await expect(details.getByText(/example-policy-agent/)).toBeVisible();
  await expect(details.getByText(/example.read/)).toBeVisible();
  await expect(details.getByText(/example-product/)).toBeVisible();
  await expect(approve).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("agent-policy-review-ready.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
});
