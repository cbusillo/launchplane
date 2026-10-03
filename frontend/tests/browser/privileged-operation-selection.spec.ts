import { expect, test } from "@playwright/test";

// Production API adapter against local HTTP fixtures; all requests must be reads.
test("readable initial tab, explicit denial, and recovery", async ({ page }, testInfo) => {
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
  await page.getByRole("button", { name: "Secret rotation", exact: true }).click();
  await expect(page.getByText("You do not have access to secret rotation plans.")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("explicit-secret-denial.png"), fullPage: true });
  await mergeTab.click();
  await expect(mergeTab).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("No changes are waiting for review")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("readable-merge-policy.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
  expect(unexpected).toEqual([]);
});
