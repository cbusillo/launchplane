import { expect, test } from "@playwright/test";

test("a carried acceptance applies to this preview and a new decision replaces it", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=carried-decision");
  const decision = page.getByLabel("Recorded decision");
  await expect(decision).toContainText("Applies to preview version aaaaaaa");
  await expect(decision).toContainText("Carried from version bbbbbbb");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-acceptance-carried.png"), fullPage: true });
  await page.getByRole("textbox").fill("Please change this feature again.");
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(decision).toContainText("Changes requested");
  await expect(decision).not.toContainText("Carried from version");
  await expect(decision.locator("blockquote")).toHaveText("Please change this feature again.");
});

test("a saved decision with pending delivery keeps the reason available to retry", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=delivery-pending");
  const reason = "Please adjust the checkout flow.\nKeep the contact details visible.";
  await page.getByRole("textbox").fill(reason);
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByLabel("Recorded decision")).toContainText("Your decision is saved, but delivery to the agent is pending.");
  await expect(page.getByRole("textbox")).toHaveValue(reason);
  await expect(page.getByLabel("Recorded decision").locator("blockquote")).toHaveText(reason);
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByLabel("Recorded decision")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-pending.png"), fullPage: true });
});

test("an earlier accepted decision cannot look like approval of the current preview", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=earlier-decision");
  await expect(page.getByLabel("Recorded decision")).toContainText("Earlier preview version bbbbbbb");
  await expect(page.getByLabel("Recorded decision")).toContainText("This decision does not apply to the current preview.");
  await expect(page.getByText("Preview version aaaaaaa", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-history.png"), fullPage: true });
});

test("delivery of a historical saved decision can be retried without a preview", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=pending-without-preview&decision_id=saved");
  const decision = page.getByLabel("Recorded decision");
  await expect(decision).toContainText("Reviewed preview version bbbbbbb");
  await expect(decision).toContainText("No preview is currently ready for comparison.");
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "View latest review" })).toBeVisible();
  await page.getByRole("button", { name: "Retry delivery", exact: true }).click();
  await expect(decision).not.toContainText("delivery to the agent is pending");
  await expect(decision).toContainText("Reviewed preview version bbbbbbb");
  await expect(decision).toContainText("Accepted");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-retried.png"), fullPage: true });
});

test("legacy feedback is shared only when the Owner explicitly sends it", async ({ page }) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=legacy-decision&decision_id=saved");
  const decision = page.getByLabel("Recorded decision");
  await expect(decision).toContainText("This saved decision has not been shared on the pull request.");
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Send feedback to the agent", exact: true }).click();
  await expect(decision).not.toContainText("has not been shared");
  await expect(decision).toContainText("Reviewed preview version aaaaaaa");
});

for (const action of ["Accept", "Request changes"]) {
  test(`a stale ${action} stays disabled until the current preview is loaded`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeVisible();
    const original = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      return (await import(modulePath)).productReviewForFixture("products");
    });
    const current = { ...original, head_sha: "b".repeat(40), preview_generation_id: "generation-next" };
    let review = original;
    let refreshFails = false;
    const submitted: Record<string, unknown>[] = [];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
    await page.route("**/v1/product-review?*", route => refreshFails
      ? route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Unavailable" } } })
      : route.fulfill({ json: review }));
    await page.route("**/v1/product-review/decisions", route => {
      submitted.push(route.request().postDataJSON());
      if (submitted.length === 1) {
        return route.fulfill({ status: 409, json: { error: { code: "product_review_preview_changed", message: "The preview changed. Refresh the review and look at the current preview before deciding again." } } });
      }
      return route.fulfill({ json: current });
    });
    await page.goto("/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308");
    await expect(page.getByText(`Preview version ${original.head_sha.slice(0, 7)}`, { exact: true })).toBeVisible();
    if (action === "Request changes") await page.getByRole("textbox").fill("Please fix the price.");
    await page.getByRole("button", { name: action, exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("The preview changed");
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Request changes", exact: true })).toBeDisabled();
    expect(submitted).toHaveLength(1);
    expect(submitted[0]).toMatchObject({ reviewed_head_sha: original.head_sha, reviewed_preview_url: original.preview_url, reviewed_generation_id: original.preview_generation_id });
    await page.screenshot({ path: testInfo.outputPath("stale-preview-refused.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // A failed refresh cannot unlock the old review.
    refreshFails = true;
    await page.getByRole("button", { name: "Refresh review" }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
    refreshFails = false;
    review = current;
    await page.getByRole("button", { name: "Refresh review" }).click();
    await expect(page.getByText(`Preview version ${current.head_sha.slice(0, 7)}`, { exact: true })).toBeVisible();
    expect(submitted).toHaveLength(1);
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Accept", exact: true }).click();
    await expect(page.getByRole("region", { name: `Decision for ${current.display_name}` }).getByRole("status")).toBeVisible();
    await expect.poll(() => submitted).toHaveLength(2);
    expect(submitted[1]).toMatchObject({ reviewed_head_sha: current.head_sha, reviewed_preview_url: current.preview_url, reviewed_generation_id: current.preview_generation_id });
  });
}
