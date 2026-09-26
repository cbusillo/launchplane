import { expect, test } from "@playwright/test";

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
