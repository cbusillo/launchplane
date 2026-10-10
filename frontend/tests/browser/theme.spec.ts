import { chromium, expect, test } from "@playwright/test";

const reviewPath = "/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308";

test("explicit themes follow review links, navigation, and other tabs", async ({ page, context }, testInfo) => {
  await page.goto(reviewPath);
  await page.getByRole("button", { name: "Use light theme", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.getByRole("button", { name: "Use dark theme", exact: true })).toBeVisible();

  const otherTab = await context.newPage();
  await otherTab.goto(reviewPath);
  await expect(otherTab.getByRole("button", { name: "Use dark theme", exact: true })).toBeVisible();
  await otherTab.screenshot({ path: testInfo.outputPath("review-light.png"), fullPage: true });
  await otherTab.goto("/ui/products?fixture=products");
  await expect(otherTab.getByRole("button", { name: "Switch to dark theme", exact: true })).toBeVisible();
  await otherTab.getByRole("button", { name: "Switch to dark theme", exact: true }).click();
  await expect(page.getByRole("button", { name: "Use light theme", exact: true })).toBeVisible();
  await otherTab.screenshot({ path: testInfo.outputPath("products-dark.png"), fullPage: true });
  const latestTab = await context.newPage();
  await latestTab.goto(reviewPath);
  await expect(latestTab.getByRole("button", { name: "Use light theme", exact: true })).toBeVisible();
  await latestTab.reload();
  await expect(latestTab.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("the saved theme applies before the application module can render", async ({ page, context }) => {
  await page.goto(reviewPath);
  await page.getByRole("button", { name: "Use light theme", exact: true }).click();
  const firstRender = await context.newPage();
  await firstRender.route("**/src/main.tsx", route => route.fulfill({ contentType: "text/javascript", body: "" }));
  await firstRender.goto(reviewPath);
  await expect(firstRender.locator("#root")).toBeEmpty();
  await expect(firstRender.locator("html")).toHaveAttribute("data-theme", "light");
});

test("a reopened browser profile remembers both explicit choices", async ({ baseURL }, testInfo) => {
  const profile = testInfo.outputPath("browser-profile");
  for (const theme of ["light", "dark"] as const) {
    const choosing = await chromium.launchPersistentContext(profile, { baseURL });
    try {
      const page = await choosing.newPage();
      await page.goto(reviewPath);
      await page.getByRole("button", { name: `Use ${theme} theme`, exact: true }).click();
    } finally {
      await choosing.close();
    }
    const returning = await chromium.launchPersistentContext(profile, { baseURL });
    try {
      const page = await returning.newPage();
      await page.goto("/ui/products?fixture=products");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.getByRole("button", { name: `Switch to ${theme === "light" ? "dark" : "light"} theme`, exact: true })).toBeVisible();
    } finally {
      await returning.close();
    }
  }
});

test("no saved choice retains the dark default without saving an implicit preference", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(reviewPath);
  await expect(page.getByRole("button", { name: "Use light theme", exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.localStorage.length)).toBe(0);
});

test("denied storage still allows theme controls", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("Storage denied", "SecurityError"); } });
  });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(reviewPath);
  await page.getByRole("button", { name: "Use light theme", exact: true }).click();
  await expect(page.getByRole("button", { name: "Use dark theme", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Use dark theme", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(errors).toEqual([]);
});
