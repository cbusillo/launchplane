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
