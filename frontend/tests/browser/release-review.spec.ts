import { expect, test } from "@playwright/test";

test("Owner cannot accept undisclosed shared component changes", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=missing");
  await expect(page.getByText("Shared website components changed outside this repository's checklist. Operator review is required.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept release" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Record operator override" })).toHaveCount(0);
});

test("Owner reviews the complete release and can request changes after accepting", async ({ page }) => {
  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => { if (request.method() !== "GET") mutations.push(request.url()); });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the testing site" })).toHaveAttribute("href", "https://testing.example.invalid/");
  await expect(page.getByText("On a phone, confirm the booking button is visible.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Request changes" })).toBeDisabled();
  await page.getByRole("button", { name: "Accept release" }).click();
  await expect(page.getByRole("status")).toHaveText("Decision recorded. The site is unchanged.");
  await page.getByRole("textbox").fill("The booking button needs a clearer label.");
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByRole("listitem").filter({ hasText: "The booking button needs a clearer label." })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});
