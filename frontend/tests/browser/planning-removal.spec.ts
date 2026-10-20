import { expect, test } from "./state";

test("removed planning links return to usable product and delivery navigation", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const errors: string[] = [];
  const mutations: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url());
  });

  for (const view of ["work-graph", "issue-inbox"]) {
    await page.goto(`/ui/engineering/${view}?fixture=products`);
    await expect(page.getByRole("heading", { level: 1, name: "This Launchplane view does not exist" })).toBeVisible();
    await page.getByRole("link", { name: "Return to Product Ops", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Products", exact: true })).toBeVisible();
  }

  await page.getByRole("link", { name: "Engineering Ops", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Platform delivery systems" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("engineering-hub.png"), fullPage: true });
  await page.locator(".engineering-hub-grid").getByRole("link", { name: /Merge train/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Merge train", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Policy target", exact: true }).selectOption({ index: 1 });
  await expect(page.getByRole("region", { name: "Controller summary", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("retained-merge-train.png"), fullPage: true });

  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  expect(errors).toEqual([]);
  expect(mutations).toEqual([]);
});
