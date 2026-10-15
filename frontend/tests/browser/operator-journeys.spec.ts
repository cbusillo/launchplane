import { expect, test, setClockTime, waitForState, type ConsoleMessage, type Page, type TestInfo } from "./state";
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
  test("anonymous operator receives the authentication prompt", { tag: "@layout" }, async ({
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

  test("anonymous Owner review preserves the exact return path", async ({ page }) => {
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          status: "rejected",
          trace_id: "browser-smoke-owner-auth-required",
          error: { code: "authentication_required", message: "Authentication is required." },
        }),
      });
    });
    const diagnostics = monitorBrowser(page, {
      allowedHttpFailures: [{ pathname: "/v1/auth/session", status: 401 }],
    });

    await page.goto(
      "/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308",
    );

    const signIn = page.getByRole("link", { name: "Sign in with GitHub" });
    await expect(signIn).toHaveAttribute(
      "href",
      /return_to=%2Fui%2Fowner-review%3Frepository%3Dexample%252Fcontrol-plane%26pull_request%3D308/,
    );
    await expect(page.getByText("Engineering Ops")).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner review fails closed before broad or exact reads for an invalid link", async ({ page }) => {
    const requestedPaths: string[] = [];
    page.on("request", (request) => requestedPaths.push(new URL(request.url()).pathname));
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/owner-review?fixture=products&repository=invalid");

    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(requestedPaths).not.toContain("/v1/products");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/current-items");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/queue");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/evaluation");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/owner-evaluation");
    expect(requestedPaths).not.toContain("/v1/product-review");
    await expect(page.getByRole("link", { name: "Engineering Ops" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Product Ops" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner review sign-out keeps the exact review return path", async ({ page }) => {
    const diagnostics = monitorBrowser(page);
    const path = "/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308";

    await page.goto(path);
    await page.getByRole("button", { name: "Sign out" }).click();

    await expect(page).toHaveURL((url) => `${url.pathname}${url.search}` === path);
    await expect(
      page.getByRole("heading", { level: 1, name: "Sign in to review this change" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner opens the preview and pull request, then accepts the change", async ({ page }) => {
    const requestedPaths: string[] = [];
    page.on("request", (request) => requestedPaths.push(new URL(request.url()).pathname));
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308",
    );

    const card = page.locator('[data-product="example-site"]');
    const preview = card.getByRole("link", { name: /Open the preview/ });
    await expect(preview).toHaveAttribute("target", "_blank");
    await expect(preview).toHaveAttribute("rel", "noreferrer");
    await expect(preview).toHaveAttribute("href", "https://site.preview.example.invalid/");
    await expect(card.getByRole("link", { name: /Pull request #308/ })).toHaveAttribute(
      "href",
      "https://github.com/example/control-plane/pull/308",
    );
    await card.getByRole("button", { name: "Accept" }).click();
    await expect(card.getByText("Decision recorded.")).toBeVisible();
    await expect(card.getByLabel("Recorded decision")).toContainText("Accepted by @example-owner");
    expect(requestedPaths).not.toContain("/v1/products");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/current-items");
    expect(requestedPaths).not.toContain("/v1/owner-acceptance/queue");
    await expect(page.getByRole("link", { name: "Engineering Ops" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner must say what should change before requesting changes", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308",
    );

    const card = page.locator('[data-product="example-site"]');
    const requestChanges = card.getByRole("button", { name: "Request changes" });
    await expect(requestChanges).toBeDisabled();
    await card.getByRole("textbox").fill("Please adjust the checkout flow.");
    await requestChanges.click();
    await expect(card.getByLabel("Recorded decision")).toContainText("Changes requested");
    await expect(card.getByLabel("Recorded decision")).toContainText(
      "Please adjust the checkout flow.",
    );
    await expect(card.getByRole("textbox")).toHaveValue("");
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner review shows the latest decision to a viewer who cannot decide", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/owner-review?fixture=products&scenario=decided&viewer=non-owner&repository=example%2Fcontrol-plane&pull_request=308",
    );

    const card = page.locator('[data-product="example-site"]');
    await expect(card.getByLabel("Recorded decision")).toContainText("Changes requested");
    await expect(card.getByText("You are not this product's Client", { exact: false })).toBeVisible();
    await expect(card.getByRole("button", { name: "Accept" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Request changes" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner review says when the product has no Owner", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/owner-review?fixture=products&scenario=no-owner&repository=example%2Fcontrol-plane&pull_request=308",
    );

    await expect(page.getByText("No Client set for this product", { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "Accept" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner review offers no decision before a preview exists", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/owner-review?fixture=products&scenario=missing-preview&repository=example%2Fcontrol-plane&pull_request=308",
    );

    const card = page.locator('[data-product="example-site"]');
    await expect(card.getByRole("link", { name: /Open the preview/ })).toHaveCount(0);
    await expect(card.getByText("No preview yet", { exact: false })).toBeVisible();
    await expect(card.getByRole("button", { name: "Accept" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("operator sees an honest empty product inventory", { tag: "@layout" }, async ({
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

  test("operator sees an honest product inventory error", { tag: "@layout" }, async ({
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

  test("operator enters the product workspace by keyboard", { tag: "@layout" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=products");
    const productsHeading = page.getByRole("heading", {
      level: 1,
      name: "Products",
    });
    // The app imports fixtures through Vite's ?import URL; the timestamp read
    // below uses a separate module request. Let the app finish before reloading.
    await expect(productsHeading).toBeFocused();
    const recordedAt = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const { productsForFixture } = await import(modulePath);
      return productsForFixture("products")[0].environments[0].provenance.refreshed_at;
    });
    await setClockTime(page, recordedAt);
    await page.reload();

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
    const expectedTones = await page.evaluate(async () => {
      const fixturePath = "/ui/src/dev-fixtures.ts";
      const signalPath = "/ui/src/product-environment-signal.ts";
      const { productsForFixture } = await import(fixturePath);
      const { environmentOperationalTone } = await import(signalPath);
      return productsForFixture("products")[0].environments.map((lane: { environment: string }) => ({
        lane: lane.environment, tone: environmentOperationalTone(lane),
      }));
    });
    const sidebarStatus = page.locator(".product-trust-dots").first();
    for (const { lane, tone } of expectedTones) {
      await expect(sidebarStatus.locator(`[data-lane="${lane}"]`)).toHaveAttribute("data-tone", tone);
    }
    const laneLabels = await sidebarStatus.locator("[data-lane]").evaluateAll(lanes =>
      lanes.map(lane => lane.getAttribute("title")));
    const accessibleLabel = await sidebarStatus.getAttribute("aria-label");
    for (const label of laneLabels) expect(accessibleLabel).toContain(label);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-workspace");
    diagnostics.assertClean();
  });

  test("operator sees active incidents across product workspaces", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products?fixture=products");

    const incidentRegion = page.getByRole("region", {
      name: "Active public ingress incidents",
    });
    await expect(incidentRegion).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "1 open incident" }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Atlas Commerce", { exact: true }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("link", { name: "Inspect incident" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-incidents-active");
    diagnostics.assertClean();
  });

  test("operator inspects incident timeline observations and deliveries", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products",
    );

    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    await expect(incidentRegion).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "Public ingress incidents" }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Critical incident", { exact: true }).first(),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Notifications active", { exact: true }).first(),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Next reminder", { exact: true }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "Material timeline" }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "Observation evidence" }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "Notification delivery" }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("heading", { name: "Reminder state" }),
    ).toBeVisible();
    await expect(
      incidentRegion.locator(".incident-history-trust .evidence-badge"),
    ).toHaveAttribute("data-state", "recorded");
    await expect(
      incidentRegion.getByRole("link", { name: "Open delivery sink" }),
    ).toBeVisible();

    await incidentRegion
      .locator('.incident-list-item[data-status="resolved"]')
      .click();
    await expect(
      incidentRegion.getByText("Resolved", { exact: true }).last(),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Resolution", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incident-detail");
    diagnostics.assertClean();
  });

  test("operator sees acknowledged incident state without reminder scheduling", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=acknowledged",
    );

    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    await expect(
      incidentRegion.getByText("Acknowledged", { exact: true }).first(),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Next reminder", { exact: true }),
    ).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(
      page,
      testInfo,
      "environment-incident-acknowledged",
    );
    diagnostics.assertClean();
  });

  test("operator sees silenced incident state at both widths", { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=silenced",
    );

    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    await expect(
      incidentRegion.getByText("Silenced", { exact: true }).first(),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Next reminder", { exact: true }),
    ).toHaveCount(0);
    const horizontalOverflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
    expect(horizontalOverflow).toBe(false);
    await assertDocumentBasics(page);
    await captureScreenshot(
      page,
      testInfo,
      "environment-incident-silenced",
    );
    diagnostics.assertClean();
  });

  test("operator sees a clean empty incident history", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=empty",
    );
    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    // Wait for the app's fixture import before the clock-setup reload.
    await expect(incidentRegion).toBeVisible();
    const recordedAt = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const { environmentForFixture } = await import(modulePath);
      return environmentForFixture("products", "atlas-commerce", "prod").provenance.refreshed_at;
    });
    await setClockTime(page, recordedAt);
    await page.reload();

    await expect(
      incidentRegion.getByText("No incidents recorded", { exact: true }),
    ).toBeVisible();
    await expect(
      incidentRegion.getByText("Incident evidence is incomplete", {
        exact: true,
      }),
    ).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-empty");
    diagnostics.assertClean();
  });

  test("operator sees stale incident history as incomplete", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=stale",
    );

    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    await expect(
      incidentRegion.getByText("Incident evidence is incomplete", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      incidentRegion.locator(".incident-history-trust .evidence-badge"),
    ).toHaveAttribute("data-state", "stale");
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-stale");
    diagnostics.assertClean();
  });

  test("operator can retry unavailable incident history", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=error",
    );

    const incidentRegion = page.getByRole("region", {
      name: "Public ingress incidents",
    });
    await expect(
      incidentRegion.getByText(
        "Incident history is intentionally unavailable.",
      ),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("button", { name: "Retry incident history" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-error");
    diagnostics.assertClean();
  });

  test("operator can review recent product activity", { tag: "@layout" }, async ({
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

  test("operator can diagnose recorded environment evidence", { tag: "@layout" }, async ({
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

  test("operator sees an honest blocked action", { tag: "@layout" }, async ({ page }, testInfo) => {
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
      promotionControl.getByRole("button", {
        name: "Dispatch workflow dry-run",
      }),
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

  test("operator inspects mixed action readiness without executing a route", { tag: "@layout" }, async ({
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
    await expect(
      authorization.getByText("Blocked", { exact: true }),
    ).toBeVisible();
    await expect(
      authorization.getByText("Supported remediation metadata", {
        exact: true,
      }),
    ).toBeVisible();

    const deployment = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Deployment" }),
    });
    await expect(
      deployment.getByText("Blocked", { exact: true }),
    ).toBeVisible();
    await expect(
      deployment.getByText("runtime_identity_status:unchecked"),
    ).toBeVisible();
    await expect(
      deployment.getByText(
        "No typed no-effect remediation is available in this browser.",
      ),
    ).toBeVisible();

    const routeBinding = page.getByRole("listitem").filter({
      has: page.getByRole("heading", { name: "Route binding" }),
    });
    await expect(
      routeBinding.getByText("Ready", { exact: true }),
    ).toBeVisible();
    await expect(
      routeBinding.getByText("Advisory — does not block", { exact: true }),
    ).toBeVisible();
    await expect(
      routeBinding.getByText("external_ingress_internals_unsupported"),
    ).toBeVisible();

    await page
      .getByLabel("Inspect exact action")
      .selectOption({ label: "Deploy prod lane · Mutation" });
    await expect(
      page.getByText("fixture.stable_deploy", { exact: true }),
    ).toBeVisible();
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

  test("operator sees ready immutable workflow preflight evidence", { tag: "@layout" }, async ({
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

  test("operator sees unsupported action readiness without execution", { tag: "@layout" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=unsupported",
    );

    await expect(
      page.locator(".readiness-result[data-state='unsupported']"),
    ).toBeVisible();
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
    await expect(
      actionSupported.getByText("No", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-unsupported");
    diagnostics.assertClean();
  });

  test("operator sees readiness authorization denial with trace evidence", { tag: "@layout" }, async ({
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
    await expect(
      page.getByText("fixture-readiness-denied", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "No prior action result is shown because readiness evidence is scoped to the exact caller, action, and lane.",
      ),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-denied");
    diagnostics.assertClean();
  });

  test("operator sees readiness service failure without stale evidence", { tag: "@layout" }, async ({
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

  test("operator sees a distinct empty readiness contract", { tag: "@layout" }, async ({
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
      page.getByText("No admin actions advertised", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-empty");
    diagnostics.assertClean();
  });

  test("operator sees a distinct readiness loading state", { tag: "@layout" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod/actions?fixture=products&readiness=slow",
    );

    await expect(
      page.getByText("Evaluating exact readiness", { exact: true }),
    ).toBeVisible();
    await waitForState(() => page.getByRole("heading", { name: "2 dimensions need attention" }).isVisible(), page).toBe(true);
    await expect(
      page.getByRole("heading", { name: "2 dimensions need attention" }),
    ).toBeVisible();
    await expect(
      page.getByText("fixture.prod_promotion", { exact: true }),
    ).toBeVisible();
    await page
      .getByLabel("Inspect exact action")
      .selectOption({ label: "Deploy prod lane · Mutation" });
    await expect(
      page.getByText("Evaluating exact readiness", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("fixture.prod_promotion", { exact: true }),
    ).toHaveCount(0);
    await waitForState(() => page.getByText("fixture.stable_deploy", { exact: true }).isVisible(), page).toBe(true);
    await expect(
      page.getByText("fixture.stable_deploy", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "action-readiness-loading-settled");
    diagnostics.assertClean();
  });

  test("operator reaches confirmation without applying a change", { tag: "@layout" }, async ({
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
    await page.getByRole("button", { name: "Add a site setting" }).click();
    await page.getByLabel("Setting name").fill("SITE_MODE");
    await page.getByLabel("Setting value").fill("full");
    await page.getByLabel("Provider keys to retire").fill("LEGACY_TUNING");
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
    await expect(
      page.getByText("Provider keys retired", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "safe-change-confirmation");
    diagnostics.assertClean();
  });

  test("operator previews a new Owner without saving", { tag: "@layout" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/products/atlas-commerce?fixture=products");

    const ownerPanel = page.getByRole("region", {
      name: "example-owner (id 9001)",
    });
    await expect(ownerPanel).toBeVisible();
    await expect(
      ownerPanel.getByText(
        "The Client can accept or request changes on previews. They can never merge or deploy.",
      ),
    ).toBeVisible();
    const saveButton = ownerPanel.getByRole("button", { name: "Save" });
    await expect(saveButton).toBeDisabled();
    await ownerPanel.getByLabel("GitHub login").fill("@new-owner");
    await ownerPanel
      .getByLabel("Change reason")
      .fill("Verify the deterministic Owner preview.");
    await ownerPanel.getByRole("button", { name: "Preview change" }).click();

    await expect(ownerPanel.getByRole("status")).toHaveText(
      "Set the Client to new-owner (id 7009).",
    );
    await expect(saveButton).toBeEnabled();
    await ownerPanel.getByLabel("GitHub login").fill("someone-else");
    await expect(saveButton).toBeDisabled();
    await expect(
      ownerPanel.getByRole("button", { name: "Clear" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "product-owner-preview");
    diagnostics.assertClean();
  });

  test("operator can inspect exact tenant admission without a browser mutation", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/tenant-admission?fixture=products");

    await expect(
      page.getByRole("heading", { level: 1, name: "Tenant admission" }),
    ).toBeFocused();
    await expect(
      page.getByRole("heading", {
        name: "Ready for controller merge",
      }),
    ).toBeVisible();
    await expect(
      page.getByText("Classification evidence unavailable", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("This page is read-only.", { exact: false }),
    ).toBeVisible();
    await expect(
      page.getByText("Manager preview approval", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Required checks" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "tenant-admission-exact-head");
    diagnostics.assertClean();
  });

  test("tenant admission never labels missing classification as engineering", async ({
    page,
  }) => {
    await page.goto("/ui/engineering/tenant-admission?fixture=empty");

    await expect(
      page.getByText("Classification evidence unavailable", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("No manager gate for engineering")).toHaveCount(
      0,
    );
    await expect(page.getByText("Engineering normal flow")).toHaveCount(0);
  });

  test("governance evidence shows machine readiness, admission, and landing", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/governance-projection?fixture=products&scenario=25",
    );

    await expect(
      page.getByRole("heading", { level: 1, name: "Governance evidence" }),
    ).toBeFocused();
    await expect(
      page.getByRole("region", { name: "Level 1 authoritative Owner acceptance" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: "Level 2 current merge readiness" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Level 3 immutable merge admission" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Separate landing outcome" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "GitHub status observations" }),
    ).toBeVisible();
    await expect(
      page.getByText("No admission recorded", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Not Observed · None target", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Non-authoritative", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(
      page,
      testInfo,
      "governance-evidence-independent-facets",
    );
    diagnostics.assertClean();
  });

  test("governance preserves the recorded landing independently of current readiness", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/governance-projection?fixture=products&scenario=15",
    );

    await expect(
      page.getByRole("region", { name: "Level 3 immutable merge admission" }),
    ).toContainText("Recorded for current target");
    await expect(
      page.getByRole("region", { name: "Separate landing outcome" }),
    ).toContainText("Landed · Current target");
    await assertDocumentBasics(page);
    await captureScreenshot(
      page,
      testInfo,
      "governance-evidence-recorded-landing",
    );
    diagnostics.assertClean();
  });

  test("governance keeps unknown technical checks visible", async ({
    page,
  }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/governance-projection?fixture=products&scenario=3",
    );
    await expect(
      page.getByText("checks_unknown", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("governance evidence preserves stale data and honest access failures", async ({
    page,
  }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/governance-projection?fixture=products&scenario=3&refresh=error",
    );
    await expect(
      page.getByText("checks_unknown", { exact: true }).first(),
    ).toBeVisible();
    await page.getByRole("button", { name: "Refresh governance" }).click();
    await expect(
      page.getByText("Cached evidence", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", {
        name: "Level 2 current merge readiness",
      }),
    ).toBeVisible();

    await page.goto("/ui/engineering/governance-projection?fixture=denied");
    await expect(
      page.getByText("Access denied", { exact: true }),
    ).toBeVisible();
    await page.goto("/ui/engineering/governance-projection?fixture=error");
    await expect(
      page.getByText("Governance evidence unavailable", { exact: true }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("privileged-operation approval remains human-governed and redacted", { tag: "@layout" }, async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/privileged-operations?fixture=products");

    await expect(
      page.getByRole("heading", {
        level: 1,
        name: "Privileged operation plans",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", {
        name: "Privileged operation plans",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");
    await expect(
      page.getByText("Review each change before approving it"),
    ).toBeVisible();
    await waitForState(() => page.getByText("Would rotate").isVisible(), page).toBe(true);
    await expect(page.getByText("18", { exact: true }).first()).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Approve plan" }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /execute/i })).toHaveCount(0);
    await expect(page.getByText(/secret-version/i)).toHaveCount(0);
    await page.getByRole("button", { name: "Merge-train policy" }).click();
    await waitForState(() => page.getByText("Managed merge-train policy", { exact: true }).isVisible(), page).toBe(true);
    await expect(
      page.getByText("Managed merge-train policy", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Candidate targets")).toBeVisible();
    await expect(page.getByText("2", { exact: true }).first()).toBeVisible();
    await expect(
      page.getByText("Bounded to merge-train policy target counts"),
    ).toBeVisible();
    await expect(page.getByText("cbusillo/launchplane:main")).toHaveCount(0);
    await expect(
      page.getByText("Review exact candidate merge-train policy"),
    ).toHaveCount(0);
    await expect(page.getByText("Authorized detail response")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Approve plan" }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /execute/i })).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(
      page,
      testInfo,
      "privileged-operation-merge-train-policy-review",
    );
    await page.getByRole("button", { name: "Access policy" }).click();
    await waitForState(() => page.getByRole("heading", { name: "Managed authorization policy review" }).isVisible(), page).toBe(true);
    await expect(
      page.getByRole("heading", { name: "Managed authorization policy review" }),
    ).toBeVisible();
    await expect(page.getByText("Added", { exact: true })).toBeVisible();
    await expect(page.getByText("1", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Prepare project evidence access" })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "access-policy-preparation");
    diagnostics.assertClean();
  });

  test("access preparation preserves its retry identity after an uncertain failure", async ({ page }) => {
    const sourceEventIds: string[] = [];
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({ json: { csrf_token: "fixture-access-policy-csrf" } });
    });
    await page.route(
      "**/v1/privileged-operations/authorization-candidates/prepare",
      async (route) => {
        const request = route.request().postDataJSON() as {
          source_event_id: string;
        };
        sourceEventIds.push(request.source_event_id);
        if (sourceEventIds.length === 1) {
          await route.fulfill({
            status: 503,
            json: {
              trace_id: "trace-access-policy-uncertain",
              error: {
                code: "service_unavailable",
                message: "Preparation outcome is uncertain.",
              },
            },
          });
          return;
        }
        if (sourceEventIds.length === 2) {
          await route.fulfill({
            json: {
              trace_id: "trace-access-policy-missing-review",
              state: "planned",
            },
          });
          return;
        }
        await route.fulfill({
          json: {
            trace_id: "trace-access-policy-replay",
            state: "already_satisfied",
          },
        });
      },
    );

    await page.goto("/ui/engineering/privileged-operations?fixture=products");
    await page.getByRole("button", { name: "Access policy" }).click();
    await waitForState(() => page.getByRole("button", { name: "Prepare evidence access" }).isVisible(), page).toBe(true);
    await page.getByRole("button", { name: "Prepare evidence access" }).click();
    await expect(page.getByText("Preparation outcome is uncertain.")).toBeVisible();
    await expect(page.getByText("Trace: trace-access-policy-uncertain")).toBeVisible();
    await page.getByRole("button", { name: "Prepare evidence access" }).click();
    await expect(
      page.getByText("The plan was prepared, but its review is not available yet."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Prepare evidence access" }).click();
    await expect(page.getByText("Project evidence access is already installed.")).toBeVisible();

    expect(sourceEventIds).toHaveLength(3);
    expect(sourceEventIds[1]).toBe(sourceEventIds[0]);
    expect(sourceEventIds[2]).toBe(sourceEventIds[0]);
  });

  test("agent proposer access prepares a pending grant without approval", async ({ page }) => {
    const requests: Array<Record<string, unknown>> = [];
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({ json: { csrf_token: "fixture-proposer-csrf" } });
    });
    await page.route("**/v1/privileged-operations/authorization-candidates/prepare", async (route) => {
      requests.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({ json: { trace_id: "trace-proposer-plan", state: "planned" } });
    });
    await page.goto("/ui/engineering/privileged-operations?fixture=products");
    await page.getByRole("button", { name: "Access policy" }).click();
    const card = page.getByRole("region", { name: "Prepare agent proposal access", exact: true });
    await waitForState(() => card.isVisible(), page).toBe(true);
    await expect(card.getByText(/Every plan remains pending until you approve it while signed in/)).toBeVisible();
    await card.getByRole("button", { name: "Prepare proposal access", exact: true }).click();
    await expect(card.getByText("The plan was prepared, but its review is not available yet.")).toBeVisible();
    expect(requests).toHaveLength(1);
    expect(Object.keys(requests[0]).sort()).toEqual(["candidate_id", "intent", "source_event_id"]);
    expect(requests[0].candidate_id).toBe("agent-policy-proposer");
    expect(requests[0].intent).toBe("add");
    await expect(card.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  });

  test("agent product setup preparation lists every product and sends their ids", async ({ page }) => {
    const requests: Array<Record<string, unknown>> = [];
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({ json: { csrf_token: "fixture-agent-product-setup-csrf" } });
    });
    await page.route(
      "**/v1/privileged-operations/authorization-candidates/prepare",
      async (route) => {
        const body = route.request().postDataJSON() as Record<string, unknown>;
        requests.push(body);
        await route.fulfill({
          json: {
            trace_id: `trace-agent-product-setup-${String(body.intent)}`,
            state: "already_satisfied",
          },
        });
      },
    );

    await page.goto("/ui/engineering/privileged-operations?fixture=products");
    await page.getByRole("button", { name: "Access policy" }).click();

    const setupCard = page
      .locator("section.privileged-operation-card")
      .filter({
        has: page.getByRole("heading", { name: "Prepare agent product setup" }),
      });
    await waitForState(() => setupCard.isVisible(), page).toBe(true);
    await expect(
      setupCard.getByText("cannot change the Client or release review", { exact: false }),
    ).toBeVisible();
    await expect(
      setupCard.getByText("changes nothing until it is approved", { exact: false }),
    ).toBeVisible();
    await waitForState(() => setupCard.getByRole("checkbox").count(), page).toBe(3);
    await expect(setupCard.getByText("Example Live")).toBeVisible();
    const addButton = setupCard.getByRole("button", { name: "Prepare product setup access" });
    await expect(addButton).toBeDisabled();

    await setupCard.getByRole("checkbox", { name: /Example Shop/ }).check();
    await addButton.click();
    await expect(
      setupCard.getByText("Agent product setup already covers exactly these products."),
    ).toBeVisible();
    await setupCard.getByRole("button", { name: "Prepare product setup removal" }).click();
    await expect(
      setupCard.getByText("Agent product setup is already removed."),
    ).toBeVisible();

    expect(requests).toHaveLength(2);
    expect(requests[0]).toMatchObject({
      candidate_id: "agent-product-setup",
      intent: "add",
      products: ["example-shop"],
    });
    expect(Object.keys(requests[0]).sort()).toEqual([
      "candidate_id",
      "intent",
      "products",
      "source_event_id",
    ]);
    expect(Object.keys(requests[1]).sort()).toEqual([
      "candidate_id",
      "intent",
      "source_event_id",
    ]);
    expect(requests[1].intent).toBe("remove");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });

  test("project evidence preparation has independent add and removal cards", async ({ page }) => {
    const requests: Array<{ candidate_id: string; intent: string; source_event_id: string }> = [];
    const attempts = new Map<string, number>();
    await page.route("**/v1/auth/session", async (route) => {
      await route.fulfill({ json: { csrf_token: "fixture-product-evidence-csrf" } });
    });
    await page.route(
      "**/v1/privileged-operations/authorization-candidates/prepare",
      async (route) => {
        const body = route.request().postDataJSON() as {
          candidate_id: string;
          intent: string;
          source_event_id: string;
        };
        requests.push(body);
        const key = `${body.candidate_id}:${body.intent}`;
        const attempt = (attempts.get(key) ?? 0) + 1;
        attempts.set(key, attempt);
        if (
          body.candidate_id === "administrator-product-evidence-read" &&
          body.intent === "add" &&
          attempt === 1
        ) {
          await route.fulfill({
            status: 503,
            json: {
              trace_id: "trace-product-evidence-uncertain",
              error: {
                code: "service_unavailable",
                message: "Preparation outcome is uncertain.",
              },
            },
          });
          return;
        }
        await route.fulfill({
          json: {
            trace_id: `trace-${body.candidate_id}-${body.intent}`,
            state: "already_satisfied",
          },
        });
      },
    );

    await page.goto("/ui/engineering/privileged-operations?fixture=products");
    await page.getByRole("button", { name: "Access policy" }).click();

    const evidenceCard = page
      .locator("section.privileged-operation-card")
      .filter({
        has: page.getByRole("heading", { name: "Prepare project evidence access" }),
      });
    await waitForState(() => evidenceCard.isVisible(), page).toBe(true);
    await expect(
      evidenceCard.getByText(
        "project and environment evidence across all current and future projects",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(evidenceCard.locator("input, select, textarea")).toHaveCount(0);

    await evidenceCard
      .getByRole("button", { name: "Prepare evidence access" })
      .click();
    await expect(evidenceCard.getByText("Preparation outcome is uncertain.")).toBeVisible();
    await evidenceCard
      .getByRole("button", { name: "Prepare evidence access" })
      .click();
    await expect(
      evidenceCard.getByText("Project evidence access is already installed."),
    ).toBeVisible();
    await evidenceCard
      .getByRole("button", { name: "Prepare evidence removal" })
      .click();
    await expect(
      evidenceCard.getByText("Project evidence access is already removed."),
    ).toBeVisible();

    expect(requests).toHaveLength(3);
    expect(requests[0].candidate_id).toBe("administrator-product-evidence-read");
    expect(requests[0].intent).toBe("add");
    expect(requests[1]).toMatchObject({
      candidate_id: "administrator-product-evidence-read",
      intent: "add",
      source_event_id: requests[0].source_event_id,
    });
    expect(requests[2]).toMatchObject({
      candidate_id: "administrator-product-evidence-read",
      intent: "remove",
    });
    expect(requests[2].source_event_id).not.toBe(requests[0].source_event_id);
    for (const request of requests) {
      expect(Object.keys(request).sort()).toEqual([
        "candidate_id",
        "intent",
        "source_event_id",
      ]);
    }
    await expect(evidenceCard.getByRole("button", { name: /approve/i })).toHaveCount(0);
    await waitForState(() => page.getByRole("button", { name: "Refresh plans" }).isVisible(), page).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await assertDocumentBasics(page);
    await captureScreenshot(page, test.info(), "access-policy-product-evidence-preparation");

    await page.goto(
      "/ui/engineering/privileged-operations?fixture=products&review=product-evidence",
    );
    await page.getByRole("button", { name: "Access policy" }).click();
    const evidenceReview = page
      .locator("article.privileged-operation-card")
      .filter({
        has: page.getByRole("heading", {
          name: "Review administrator product evidence access",
        }),
      });
    await waitForState(() => evidenceReview.isVisible(), page).toBe(true);
    await expect(
      evidenceReview
        .getByText("all current and future projects", { exact: false })
        .first(),
    ).toBeVisible();
    await expect(
      evidenceReview.locator("details").getByText("Added", { exact: true }),
    ).toBeHidden();
    await evidenceReview.getByText("Technical details", { exact: true }).click();
    await expect(
      evidenceReview.locator("details").getByText("Added", { exact: true }),
    ).toBeVisible();
    await expect(
      evidenceReview.locator("details").getByText("2", { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh plans" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await assertDocumentBasics(page);
  });
});

async function fillOrdinaryTargetForm(card: ReturnType<Page["locator"]>): Promise<void> {
  await card.getByRole("combobox", { name: "Repository" }).selectOption("1001");
  await card.getByRole("textbox", { name: "Base branch" }).fill("main");
  await card.getByRole("textbox", { name: "Enqueue label" }).fill("merge-train");
  await card.getByRole("textbox", { name: "Blocked label" }).fill("merge-train-blocked");
  await card.getByRole("combobox", { name: "Merge method" }).selectOption("merge");
  await card.getByRole("combobox", { name: "Engineering review" }).selectOption("advisory");
  await card.getByRole("combobox", { name: "Failure handling" }).selectOption("pause_train");
  await card.getByRole("combobox", { name: "Require the enqueue label" }).selectOption("true");
  await card.getByRole("checkbox", { name: "Repository owner" }).check();
  await card.getByRole("combobox", { name: "Identity kind" }).selectOption("github_app");
  await card.getByRole("textbox", { name: "Identity name" }).fill("merge-train-app");
}

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
