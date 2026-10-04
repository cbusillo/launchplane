import { expect, test } from "@playwright/test";

test("Owner cannot accept undisclosed shared component changes", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=missing");
  await expect(page.getByText("Shared website components changed outside this repository's checklist. Admin review is required.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept release" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Record admin approval override" })).toHaveCount(0);
  await expect(page.getByText("Releases are on hold for Example site.")).toBeVisible();
});

test("An unavailable checklist names its reason code and trace ID", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=empty");
  await expect(page.getByText("Production has no recorded deployed version.", { exact: false })).toBeVisible();
  await expect(page.locator("code", { hasText: "production_identity_missing" })).toBeVisible();
  await expect(page.locator("code", { hasText: "fixture-release-review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept release" })).toHaveCount(0);
});

test("Owner reviews the complete release and can request changes after accepting", async ({ page }, testInfo) => {
  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") mutations.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the testing site" })).toHaveAttribute("href", "https://testing.example.invalid/");
  const checks = page.locator(".release-review-checklist > li");
  await expect(checks).toHaveCount(1);
  await expect(checks.getByText("On a phone, confirm the booking button is visible.", { exact: false })).toBeVisible();
  await expect(checks.locator(".release-review-changes > li")).toHaveCount(2);
  const untested = page.locator(".release-review-untested");
  await expect(untested.getByText("2 changes need nothing from you")).toBeVisible();
  await expect(untested.getByText("Speed up CI")).toBeHidden();
  await expect(page.getByRole("button", { name: "Request changes" })).toBeDisabled();
  const versions = page.locator(".release-review-technical");
  await expect(versions.locator("dl")).toBeHidden();
  await expect(page.getByText("Accepting puts this version on the live site, Example site (www.example.invalid).")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("owner-initial.png"), fullPage: true });
  await page.getByRole("button", { name: "Accept release" }).click();
  await expect(page.getByRole("status")).toContainText("Launchplane is starting it");
  await expect(page.getByRole("region", { name: "Release progress" }).getByRole("listitem")).toHaveCount(5);
  await expect(page.getByRole("region", { name: "Latest release decision" })).toContainText("site-owner");
  await page.screenshot({ path: testInfo.outputPath("owner-accepted.png"), fullPage: true });
  const feedback = "The booking button needs a clearer label.\nKeep the contact link visible on a phone.";
  await page.getByRole("textbox").fill(feedback);
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByRole("region", { name: "Latest release decision" }).getByRole("blockquote")).toHaveText(feedback);
  await versions.getByText("Technical details", { exact: true }).click();
  await expect(versions.locator("code")).toHaveText(["a".repeat(40), "b".repeat(40)]);
  await page.screenshot({ path: testInfo.outputPath("owner-changes-requested.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});

test("Operator records a separate reasoned override without Owner controls", async ({ page }, testInfo) => {
  const mutations: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") mutations.push(request.url()); });
  await page.goto("/ui/owner-review?product=example-site&fixture=operator");
  await expect(page.getByRole("button", { name: "Accept release" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request changes" })).toHaveCount(0);
  const override = page.getByRole("region", { name: "Admin Approval Override", exact: true });
  const record = override.getByRole("button");
  await expect(record).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("operator-initial.png"), fullPage: true });
  await override.getByRole("textbox").fill("Reviewed the missing instructions with the Client.");
  await record.click();
  await expect(page.getByRole("status")).toBeVisible();
  const latest = page.getByRole("region", { name: "Latest release decision" });
  await expect(latest).toContainText("site-operator");
  await expect(latest.getByRole("blockquote")).toHaveText("Reviewed the missing instructions with the Client.");
  await page.screenshot({ path: testInfo.outputPath("operator-recorded.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
});

test("A saved decision exposes publication failure and allows retry", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=error");
  const latest = page.getByRole("region", { name: "Latest release decision" });
  await expect(latest.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Accept release", exact: true }).click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(latest.getByRole("alert")).toHaveCount(0);
});
