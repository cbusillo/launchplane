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
    await expect(heading).toBeFocused();
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

  test("operator sees an honest empty product inventory", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=empty");

    const heading = page.getByRole("heading", {
      level: 1,
      name: "No products are owned by Launchplane yet",
    });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();
    await expect(
      page.getByText(
        "This browser does not invent a sample product or infer provider state.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Refresh product inventory" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-inventory-empty");
    diagnostics.assertClean();
  });

  test("operator sees an honest product inventory error", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=error");

    const heading = page.getByRole("heading", {
      level: 1,
      name: "Product inventory unavailable",
    });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();
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
    await expect(page.locator(".product-trust-dots").first()).toHaveAttribute(
      "aria-label",
      "Testing data trust: verified; Production data trust: verified",
    );
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-workspace");
    diagnostics.assertClean();
  });

  test("operator can review recent product activity", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products/atlas-commerce/activity?fixture=products");

    await expect(
      page.getByRole("heading", { level: 1, name: "Recent activity" }),
    ).toBeFocused();
    await expect(
      page.getByRole("heading", {
        name: "Production TLS verification failed",
      }),
    ).toBeVisible();
    await expect(
      page.getByText("This is a recent window, not a complete audit export."),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-activity");
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
    const promotionControl = page.getByRole("region", {
      name: "Review evidence, then dispatch the workflow",
    });
    const blockedWorkflow = promotionControl
      .locator(".promotion-availability")
      .filter({ hasText: "Workflow live" });
    await expect(
      blockedWorkflow.getByText("Blocked", { exact: true }),
    ).toBeVisible();
    await expect(
      blockedWorkflow.getByText("Caller is not authorized for this action.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      promotionControl.getByRole("button", { name: "Dispatch workflow dry-run" }),
    ).toBeDisabled();
    await expect(
      promotionControl.getByRole("button", { name: "Dispatch live promotion" }),
    ).toBeDisabled();
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

  test("operator inspects mixed action readiness without executing a route", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products",
    );

    await expect(
      page.getByRole("heading", { name: "Exact lane and action evidence" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "2 dimensions need attention" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Browser identity limitation" }),
    ).toContainText("Browser evidence view — not workflow authorization");

    const authorization = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Authorization" }),
    });
    await expect(authorization.getByText("Blocked", { exact: true })).toBeVisible();
    await expect(
      authorization.getByText("Supported remediation metadata", { exact: true }),
    ).toBeVisible();

    const deployment = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Deployment" }),
    });
    await expect(deployment.getByText("Blocked", { exact: true })).toBeVisible();
    await expect(deployment.getByText("runtime_identity_status:unchecked")).toBeVisible();
    await expect(
      deployment.getByText("No typed no-effect remediation is available in this browser."),
    ).toBeVisible();

    const routeBinding = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Route binding" }),
    });
    await expect(routeBinding.getByText("Ready", { exact: true })).toBeVisible();
    await expect(
      routeBinding.getByText("Advisory — does not block", { exact: true }),
    ).toBeVisible();
    await expect(
      routeBinding.getByText("external_ingress_internals_unsupported"),
    ).toBeVisible();

    await page
      .getByLabel("Inspect exact action")
      .selectOption({ label: "Deploy prod lane · Mutation" });
    await expect(page.getByText("fixture.stable_deploy", { exact: true })).toBeVisible();
    if (testInfo.project.name === "narrow") {
      const hasHorizontalOverflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth,
      );
      expect(hasHorizontalOverflow).toBe(false);
    }
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-mixed");
    diagnostics.assertClean();
  });

  test("operator sees ready immutable workflow preflight evidence", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=ready",
    );

    await expect(
      page.getByRole("heading", {
        name: "All required readiness dimensions pass",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "GitHub Actions workflow identity" }),
    ).toContainText("Immutable workflow identity evaluated");
    const needsAttention = page
      .locator(".readiness-summary-strip > div")
      .filter({ hasText: "need attention" });
    await expect(needsAttention.getByText("0", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("list", { name: "Readiness dimensions" }),
    ).not.toContainText("Blocked");
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-ready");
    diagnostics.assertClean();
  });

  test("operator sees unsupported action readiness without execution", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=unsupported",
    );

    await expect(page.locator(".readiness-result[data-state='unsupported']")).toBeVisible();
    await expect(
      page.getByText(
        "The requested action has no instance-scoped operational readiness contract.",
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Choose an instance-scoped driver action with declared readiness requirements.",
      ),
    ).toBeVisible();
    const actionSupported = page
      .locator(".readiness-summary-strip > div")
      .filter({ hasText: "action supported" });
    await expect(actionSupported.getByText("No", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-unsupported");
    diagnostics.assertClean();
  });

  test("operator sees readiness authorization denial with trace evidence", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=denied",
    );

    await expect(
      page.getByText(
        "This browser session cannot read operational readiness evidence.",
      ),
    ).toBeVisible();
    await expect(page.getByText("fixture-readiness-denied", { exact: true })).toBeVisible();
    await expect(
      page.getByText(
        "No prior action result is shown because readiness evidence is scoped to the exact caller, action, and lane.",
      ),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-denied");
    diagnostics.assertClean();
  });

  test("operator sees readiness service failure without stale evidence", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=error",
    );

    await expect(
      page.getByText("Operational readiness is intentionally unavailable."),
    ).toBeVisible();
    await expect(
      page.getByText("fixture-readiness-unavailable", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("list", { name: "Readiness dimensions" }),
    ).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-error");
    diagnostics.assertClean();
  });

  test("operator sees a distinct empty readiness contract", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/beacon-docs/environments/testing/actions?fixture=missing",
    );

    await expect(
      page.getByText("No exact action readiness contract", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("No operator actions advertised", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-empty");
    diagnostics.assertClean();
  });

  test("operator sees a distinct readiness loading state", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=slow",
    );

    await expect(page.getByText("Evaluating exact readiness", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "2 dimensions need attention" }),
    ).toBeVisible();
    await expect(page.getByText("fixture.prod_promotion", { exact: true })).toBeVisible();
    await page
      .getByLabel("Inspect exact action")
      .selectOption({ label: "Deploy prod lane · Mutation" });
    await expect(page.getByText("Evaluating exact readiness", { exact: true })).toBeVisible();
    await expect(page.getByText("fixture.prod_promotion", { exact: true })).toHaveCount(0);
    await expect(page.getByText("fixture.stable_deploy", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-loading-settled");
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
    await confirmation.getByRole("checkbox").check();
    await expect(
      confirmation.getByRole("button", { name: "Apply reviewed change" }),
    ).toBeEnabled();
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
  const horizontalOverflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth,
  );
  expect(horizontalOverflow, "horizontal document overflow").toBe(false);
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
