import {
  expect,
  test,
  type ConsoleMessage,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

interface AllowedHttpFailure {
  pathname: string;
  status: number;
}

interface BrowserDiagnosticsOptions {
  allowedHttpFailures?: AllowedHttpFailure[];
}

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const screenshotRoot = resolve(
  frontendRoot,
  "../tmp/browser-smoke/screenshots",
);

test.describe("operator journeys", () => {
  test("anonymous operator receives the authentication prompt", async ({
    page,
  }, testInfo) => {
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          status: "rejected",
          trace_id: "browser-smoke-auth-required",
          error: {
            code: "authentication_required",
            message: "Authentication is required.",
          },
        }),
      });
    });
    const diagnostics = monitorBrowser(page, {
      allowedHttpFailures: [{ pathname: "/v1/auth/session", status: 401 }],
    });

    await page.goto("/ui/products");

    const heading = page.getByRole("heading", {
      level: 1,
      name: "Sign in to operate products",
    });
    await expect(heading).toBeVisible();
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.locator('[aria-live="polite"]')).toBeVisible();
    const signIn = page.getByRole("link", { name: "Sign in with GitHub" });
    await expect(signIn).toHaveAttribute(
      "href",
      /\/auth\/github\/login\?return_to=/,
    );
    await page.keyboard.press("Tab");
    await expect(signIn).toBeFocused();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "anonymous-auth-prompt");
    diagnostics.assertClean();
  });

  test("operator sees an honest product inventory error", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=error");

    await expect(
      page.getByRole("heading", {
        level: 1,
        name: "Product inventory unavailable",
      }),
    ).toBeFocused();
    await expect(
      page.getByText(
        "The fixture product inventory is intentionally unavailable.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-inventory-error");
    diagnostics.assertClean();
  });

  test("operator enters the product workspace by keyboard", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=products");

    const productsHeading = page.getByRole("heading", {
      level: 1,
      name: "Products",
    });
    await expect(productsHeading).toBeFocused();
    const atlasLink = page
      .getByRole("list", { name: "Launchplane products" })
      .getByRole("link", { name: /Atlas Commerce/ });
    await expect(atlasLink).toBeVisible();
    await atlasLink.focus();
    await page.keyboard.press("Enter");
    const workspaceHeading = page.getByRole("heading", {
      level: 1,
      name: "Atlas Commerce",
    });
    await expect(workspaceHeading).toBeFocused();
    await expect(
      page.getByRole("link", { name: /Production/ }).first(),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-workspace");
    diagnostics.assertClean();
  });

  test("operator can diagnose recorded environment evidence", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/diagnostics?fixture=products",
    );

    await expect(
      page.getByRole("heading", { level: 1, name: "Diagnostics" }),
    ).toBeFocused();
    await expect(
      page.getByRole("heading", { name: "Technical evidence and identifiers" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Provider-recorded topology" }),
    ).toBeVisible();
    await expect(
      page.getByText("TLS terminator", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-diagnostics");
    diagnostics.assertClean();
  });

  test("operator sees an honest blocked action", async ({ page }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products",
    );

    await expect(
      page.getByRole("heading", { level: 1, name: "Actions" }),
    ).toBeFocused();
    const blockedAction = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Dispatch promote workflow" }),
    });
    await expect(
      blockedAction.getByText("Launchplane blockers", { exact: true }),
    ).toBeVisible();
    await expect(
      blockedAction.getByText("Caller is not authorized for this action.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      blockedAction.getByText("Blocked", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "blocked-action");
    diagnostics.assertClean();
  });

  test("operator reaches confirmation without applying a change", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/runtime-settings?fixture=products",
    );

    await expect(
      page.getByRole("heading", { level: 1, name: "Runtime settings" }),
    ).toBeFocused();
    const publicOriginField = page
      .locator(".product-config-field")
      .filter({ hasText: "PUBLIC_ORIGIN" });
    await publicOriginField.getByRole("checkbox").check();
    await publicOriginField
      .getByLabel("New value")
      .fill("https://example.invalid");
    await page
      .getByLabel("Change reason")
      .fill("Verify the deterministic browser dry-run.");
    await page.getByRole("button", { name: "Run dry-run" }).click();

    const confirmation = page.getByRole("region", {
      name: "Apply confirmation",
    });
    await expect(
      confirmation.getByRole("heading", {
        name: "Confirm the reviewed change",
      }),
    ).toBeVisible();
    await expect(confirmation.getByRole("checkbox")).not.toBeChecked();
    await expect(
      confirmation.getByRole("button", { name: "Apply reviewed change" }),
    ).toBeDisabled();
    await expect(
      page.getByText("Dry-run evidence", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "safe-change-confirmation");
    diagnostics.assertClean();
  });
});

function monitorBrowser(
  page: Page,
  options: BrowserDiagnosticsOptions = {},
): { assertClean: () => void } {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const requestFailures: string[] = [];
  const responseFailures: string[] = [];
  const mutationRequests: string[] = [];
  const allowedHttpFailures = options.allowedHttpFailures ?? [];

  page.on("console", (message) => {
    if (
      message.type() !== "error" ||
      isAllowedConsoleError(message, allowedHttpFailures)
    ) {
      return;
    }
    consoleErrors.push(
      `${message.location().url || "page"}: ${message.text()}`,
    );
  });
  page.on("pageerror", (error) =>
    pageErrors.push(error.stack ?? error.message),
  );
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.method() !== "HEAD") {
      const url = new URL(request.url());
      mutationRequests.push(`${request.method()} ${url.pathname}`);
    }
  });
  page.on("requestfailed", (request) => {
    const url = new URL(request.url());
    const failure = request.failure()?.errorText ?? "unknown failure";
    if (
      url.pathname === "/v1/auth/session" &&
      allowedHttpFailures.some(({ pathname }) => pathname === url.pathname) &&
      failure.includes("ERR_ABORTED")
    ) {
      return;
    }
    requestFailures.push(`${request.method()} ${url.pathname}: ${failure}`);
  });
  page.on("response", (response) => {
    if (response.status() < 400) {
      return;
    }
    const url = new URL(response.url());
    const allowed = allowedHttpFailures.some(
      ({ pathname, status }) =>
        pathname === url.pathname && status === response.status(),
    );
    if (!allowed) {
      responseFailures.push(
        `${response.status()} ${response.request().method()} ${url.pathname}`,
      );
    }
  });

  return {
    assertClean() {
      expect(consoleErrors, "unexpected browser console errors").toEqual([]);
      expect(pageErrors, "unexpected uncaught page errors").toEqual([]);
      expect(mutationRequests, "unexpected browser mutation requests").toEqual(
        [],
      );
      expect(requestFailures, "unexpected failed browser requests").toEqual([]);
      expect(responseFailures, "unexpected HTTP error responses").toEqual([]);
    },
  };
}

function isAllowedConsoleError(
  message: ConsoleMessage,
  allowedHttpFailures: AllowedHttpFailure[],
): boolean {
  if (!message.text().includes("Failed to load resource")) {
    return false;
  }
  const locationUrl = message.location().url;
  if (!locationUrl) {
    return false;
  }
  const location = new URL(locationUrl);
  return allowedHttpFailures.some(
    ({ pathname, status }) =>
      pathname === location.pathname && message.text().includes(String(status)),
  );
}

async function assertDocumentBasics(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  const duplicateIds = await page.locator("[id]").evaluateAll((elements) => {
    const counts = new Map<string, number>();
    for (const element of elements) {
      const id = element.id;
      counts.set(id, (counts.get(id) ?? 0) + 1);
    }
    return [...counts.entries()]
      .filter(([, count]) => count > 1)
      .map(([id]) => id)
      .sort();
  });
  expect(duplicateIds, "duplicate document IDs").toEqual([]);
}

async function captureScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const screenshotPath = resolve(
    screenshotRoot,
    testInfo.project.name,
    `${name}.png`,
  );
  await mkdir(dirname(screenshotPath), { recursive: true });
  await page.evaluate(() => {
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: screenshotPath,
    animations: "disabled",
    caret: "hide",
    fullPage: true,
  });
  await testInfo.attach(name, {
    path: screenshotPath,
    contentType: "image/png",
  });
}
