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
    await expect(incidentRegion.getByText("Atlas Commerce", { exact: true })).toBeVisible();
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

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
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
    await expect(incidentRegion.getByText("Next reminder", { exact: true })).toBeVisible();
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
    await expect(incidentRegion.locator(".incident-history-trust .evidence-badge")).toHaveAttribute(
      "data-state",
      "recorded",
    );
    await expect(
      incidentRegion.getByRole("link", { name: "Open delivery sink" }),
    ).toBeVisible();

    await incidentRegion.locator('.incident-list-item[data-status="resolved"]').click();
    await expect(incidentRegion.getByText("Resolved", { exact: true }).last()).toBeVisible();
    await expect(incidentRegion.getByText("Resolution", { exact: true })).toBeVisible();
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

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
    await expect(incidentRegion.getByText("Acknowledged", { exact: true }).first()).toBeVisible();
    await expect(incidentRegion.getByText("Next reminder", { exact: true })).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incident-acknowledged");
    diagnostics.assertClean();
  });

  test("operator sees silenced incident state at narrow width", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=silenced",
    );

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
    await expect(incidentRegion.getByText("Silenced", { exact: true }).first()).toBeVisible();
    await expect(incidentRegion.getByText("Next reminder", { exact: true })).toHaveCount(0);
    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(horizontalOverflow).toBe(false);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incident-silenced-narrow");
    diagnostics.assertClean();
  });

  test("operator sees a clean empty incident history", async ({ page }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=empty",
    );

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
    await expect(incidentRegion.getByText("No incidents recorded", { exact: true })).toBeVisible();
    await expect(incidentRegion.getByText("Incident evidence is incomplete", { exact: true })).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-empty");
    diagnostics.assertClean();
  });

  test("operator sees stale incident history as incomplete", async ({ page }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=stale",
    );

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
    await expect(
      incidentRegion.getByText("Incident evidence is incomplete", { exact: true }),
    ).toBeVisible();
    await expect(incidentRegion.locator(".incident-history-trust .evidence-badge")).toHaveAttribute(
      "data-state",
      "stale",
    );
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-stale");
    diagnostics.assertClean();
  });

  test("operator can retry unavailable incident history", async ({ page }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/products/atlas-commerce/environments/prod?fixture=products&incident=error",
    );

    const incidentRegion = page.getByRole("region", { name: "Public ingress incidents" });
    await expect(
      incidentRegion.getByText("Incident history is intentionally unavailable."),
    ).toBeVisible();
    await expect(
      incidentRegion.getByRole("button", { name: "Retry incident history" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "environment-incidents-error");
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

  test("operator can inspect exact tenant admission without a browser mutation", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/tenant-admission?fixture=products");

    await expect(
      page.getByRole("heading", { level: 1, name: "Tenant admission" }),
    ).toBeFocused();
    await expect(
      page.getByRole("heading", { name: "Waiting for one current admission path" }),
    ).toBeVisible();
    await expect(page.getByText("Agent authoring disabled", { exact: true })).toBeVisible();
    await expect(
      page.getByText("Manager preview approval", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Repository-owner technical waiver", { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Required checks" })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "tenant-admission-exact-head");
    diagnostics.assertClean();
  });

  test("tenant admission remains readable at narrow width", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto("/ui/engineering/tenant-admission?fixture=products");

    await expect(
      page.getByRole("heading", { level: 1, name: "Tenant admission" }),
    ).toBeFocused();
    await expect(
      page.getByRole("heading", { name: "One current human action is enough" }),
    ).toBeVisible();
    await expect(page.getByText("ci-gate", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "tenant-admission-narrow");
    diagnostics.assertClean();
  });

  test("tenant admission never labels missing classification as engineering", async ({
    page,
  }) => {
    await page.goto("/ui/engineering/tenant-admission?fixture=empty");

    await expect(
      page.getByText("Classification evidence unavailable", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("No manager gate for engineering")).toHaveCount(0);
    await expect(page.getByText("Engineering normal flow")).toHaveCount(0);
  });

  test("governance evidence keeps Owner judgment, readiness, admission, and landing separate", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=25");

    await expect(
      page.getByRole("heading", { level: 1, name: "Governance evidence" }),
    ).toBeFocused();
    await expect(page.getByRole("region", { name: "Level 1 historical Owner judgment" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Level 2 current merge readiness" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Level 3 immutable merge admission" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Separate landing outcome" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Neutral advisory observations" })).toBeVisible();
    const owner = page.getByRole("region", { name: "Level 1 historical Owner judgment" });
    await expect(owner).toContainText("Owner product judgment");
    await expect(owner).toContainText("authorizes: []");
    await expect(owner).toContainText("product_review_accepted");
    await expect(page.getByText("No admission recorded", { exact: true })).toBeVisible();
    await expect(page.getByText("Not Observed", { exact: true })).toBeVisible();
    await expect(page.getByText("Neutral · non-blocking", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "governance-evidence-independent-facets");
    diagnostics.assertClean();
  });

  test("governance scenario 15 preserves landed history after Owner revocation", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=15");

    const owner = page.getByRole("region", { name: "Level 1 historical Owner judgment" });
    await expect(owner.getByText("Revoked", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("region", { name: "Level 3 immutable merge admission" })).toContainText("Admitted");
    await expect(page.getByRole("region", { name: "Separate landing outcome" })).toContainText("Landed");
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "governance-evidence-revoked-after-landing");
    diagnostics.assertClean();
  });

  test("governance mixed products and unknown checks retain every reason", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=24");

    await expect(page.getByText("example-secondary", { exact: true })).toBeVisible();
    await expect(page.getByText("owner_changes_requested", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Blocked Owner Evidence", { exact: true }).first()).toBeVisible();
    await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=3");
    await expect(page.getByText("checks_unknown", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("governance evidence preserves stale data and honest access failures", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/governance-projection?fixture=products&scenario=20&refresh=error",
    );
    await expect(page.getByText("preview_isolation_insufficient", { exact: true }).first()).toBeVisible();
    await page.getByRole("button", { name: "Refresh governance" }).click();
    await expect(page.getByText("Cached evidence", { exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "Level 1 historical Owner judgment" })).toBeVisible();

    await page.goto("/ui/engineering/governance-projection?fixture=denied");
    await expect(page.getByText("Access denied", { exact: true })).toBeVisible();
    await page.goto("/ui/engineering/governance-projection?fixture=error");
    await expect(page.getByText("Governance evidence unavailable", { exact: true })).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("operator reviews Recorded Owner acceptance list without mutation controls", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    await expect(
      page.getByRole("heading", { level: 1, name: "Owner product review" }),
    ).toBeFocused();
    await expect(
      page.getByRole("link", { name: "Owner product review", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Shadow mode — product review evidence only"),
    ).toBeVisible();
    const recordedHistory = page.getByLabel("Recorded Owner acceptance history");
    await expect(recordedHistory.getByText(/Recorded/).first()).toBeVisible();
    await expect(recordedHistory.getByText("No Owner mutation controls exposed").first()).toBeVisible();
    await expect(
      recordedHistory.getByRole("button", { name: /^(accept|request changes|revoke)$/i }),
    ).toHaveCount(0);
    await expect(recordedHistory.getByText(/verification required/i).first()).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "owner-acceptance-read-only");
    diagnostics.assertClean();
  });

  test("Owner acceptance empty state distinguishes no recorded candidates", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=empty");

    await expect(page.getByText("No recorded entries", { exact: true })).toBeVisible();
    await expect(page.getByText("No entries match the current filters")).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner acceptance current items load without lookup", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    await expect(page.getByLabel("Current Owner product review items")).toBeVisible();
    await expect(page.getByText("Fixture pull request requiring current Owner review")).toBeVisible();
    await expect(page.getByRole("region", { name: /Owner product review for/ })).toBeVisible();
    const currentItemVisuals = await page.locator(".engineering-owner-current-item").first().evaluate(
      (element) => {
        const itemStyle = getComputedStyle(element);
        const listStyle = getComputedStyle(element.parentElement!);
        return {
          borderStyle: itemStyle.borderTopStyle,
          borderWidth: itemStyle.borderTopWidth,
          boxShadow: itemStyle.boxShadow,
          rowGap: listStyle.rowGap,
        };
      },
    );
    expect(currentItemVisuals).toEqual({
      borderStyle: "solid",
      borderWidth: "1px",
      boxShadow: "none",
      rowGap: "16px",
    });
    await expect(page.getByRole("textbox", { name: "Repository (owner/repo)" })).toHaveCount(0);
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner acceptance exact lookup remains a collapsed fallback", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    await page.getByText("Exact lookup fallback", { exact: true }).click();
    await expect(page.getByText("Exact lookup — Current evaluation")).toBeVisible();
    await expect(page.getByRole("button", { name: "Look up" })).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Repository (owner/repo)" }),
    ).toBeVisible();
    await expect(
      page.getByRole("spinbutton", { name: "Pull request number" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("engineering viewer sees Current evidence without Owner controls", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=empty");
    await page.getByText("Exact lookup fallback", { exact: true }).click();
    await page.getByRole("textbox", { name: "Repository (owner/repo)" }).fill("example/site");
    await page.getByRole("spinbutton", { name: "Pull request number" }).fill("308");
    await page.getByRole("button", { name: "Look up" }).click();

    await expect(page.getByLabel("Current evaluation result")).toBeVisible();
    await expect(page.getByText("Read-only product review visibility")).toBeVisible();
    await expect(page.getByRole("region", { name: /Owner product review for/ })).toHaveCount(0);
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "owner-acceptance-current-read-only");
    diagnostics.assertClean();
  });

  test("current Owner acceptance item records an exact shadow action", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    const panel = page.getByRole("region", { name: /Owner product review for/ });
    await expect(panel).toBeVisible();
    await expect(panel.getByText("Product review only.", { exact: true })).toBeVisible();
    await expect(panel.getByText(/does not indicate that technical checks passed/i)).toBeVisible();
    await expect(panel.getByText(/make the pull request merge-ready/i)).toBeVisible();
    await expect(panel.getByText(/or authorize production/i)).toBeVisible();
    await panel.getByRole("button", { name: "Record product review" }).click();
    await expect(panel.getByText(/recorded in shadow mode/i)).toBeVisible();
    await expect(panel.getByText(/No merge or production authority/i)).toBeVisible();
    diagnostics.assertClean();
  });

  test("resolving requested Owner changes requires structured evidence", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    const panel = page.getByRole("region", { name: /Owner product review for/ });
    const submit = panel.getByRole("button", { name: "Record product review" });
    await panel.getByRole("combobox").selectOption("changes_requested");
    await panel
      .getByRole("textbox", { name: "Reason" })
      .fill("Please clarify the product behavior.");
    await submit.click();

    await expect(panel.getByText("Resolution evidence required.", { exact: true })).toBeVisible();
    await expect(submit).toBeDisabled();
    await panel
      .getByRole("textbox", { name: "Resolution summary" })
      .fill("The requested behavior is now explicit and covered by the owner flow test.");
    await panel
      .getByRole("textbox", { name: "Resolved evidence references" })
      .fill("test:owner-flow\nrecord:product-spec-17");
    await expect(submit).toBeEnabled();
    await captureScreenshot(page, testInfo, "owner-product-review-resolution-evidence");
    await submit.click();

    await expect(panel.getByText("Resolution evidence required.", { exact: true })).toHaveCount(0);
    await expect(panel.getByText(/recorded in shadow mode/i)).toBeVisible();
    diagnostics.assertClean();
  });

  test("non-Owner sees exact review evidence without actionable controls", async ({
    page,
  }, testInfo) => {
    const diagnostics = monitorBrowser(page);

    await page.goto(
      "/ui/engineering/owner-acceptance?fixture=products&viewer=non-owner",
    );

    await expect(page.getByText("Not a current product Owner", { exact: true })).toBeVisible();
    await expect(page.getByText(/only a current product Owner can record/i)).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Record product review" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: /Owner product review unavailable for/ }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    await captureScreenshot(page, testInfo, "owner-product-review-non-owner");
    diagnostics.assertClean();
  });

  test("request changes and revoke require explicit human input", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    const panel = page.getByRole("region", { name: /Owner product review for/ });
    const submit = panel.getByRole("button", { name: "Record product review" });
    await panel.getByRole("combobox").selectOption("changes_requested");
    await expect(submit).toBeDisabled();
    await panel.getByRole("textbox", { name: "Reason" }).fill("Please correct the product flow.");
    await expect(submit).toBeEnabled();
    await panel.getByRole("combobox").selectOption("revoked");
    await expect(submit).toBeDisabled();
    await panel.getByRole("checkbox").check();
    await expect(submit).toBeEnabled();
    diagnostics.assertClean();
  });

  test("binding drift refreshes Current evidence without auto-resubmitting", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=missing");
    const panel = page.getByRole("region", { name: /Owner product review for/ });
    await panel.getByRole("combobox").selectOption("revoked");
    await panel.getByRole("textbox", { name: "Reason" }).fill("Revoke the reviewed binding.");
    await panel.getByRole("checkbox").check();
    await panel.getByRole("button", { name: "Record product review" }).click();

    await expect(page.getByText(/reviewed binding changed/i)).toBeVisible();
    await expect(page.getByText(/explicitly submit again/i)).toBeVisible();
    const refreshedPanel = page.getByRole("region", { name: /Owner product review for/ });
    await expect(refreshedPanel.getByText("bbbbbbbbbbbb", { exact: true })).toBeVisible();
    await expect(refreshedPanel.getByRole("combobox")).toHaveValue("accepted");
    await expect(refreshedPanel.getByRole("checkbox")).toHaveCount(0);
    await expect(refreshedPanel.getByRole("button", { name: "Record product review" })).toBeEnabled();
    diagnostics.assertClean();
  });

  test("Owner acceptance repository filter is labeled as substring", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    await expect(
      page.getByRole("searchbox", { name: "Filter by repository substring" }),
    ).toBeVisible();
    await assertDocumentBasics(page);
    diagnostics.assertClean();
  });

  test("Owner acceptance repository filter retains focus until submitted", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    const repositoryFilter = page.getByRole("searchbox", {
      name: "Filter by repository substring",
    });
    await repositoryFilter.click();
    await repositoryFilter.pressSequentially("unrelated/repository");
    await expect(repositoryFilter).toBeFocused();
    await expect(repositoryFilter).toHaveValue("unrelated/repository");
    await repositoryFilter.press("Enter");

    await expect(page.getByText("No recorded entries", { exact: true })).toBeVisible();
    await expect(
      page.getByText("Matching filters").locator("..").getByText("0", { exact: true }),
    ).toBeVisible();

    diagnostics.assertClean();
  });

  test("Owner acceptance filter by status reduces displayed entries", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    // All statuses: multiple entries visible
    await expect(page.getByRole("listitem")).not.toHaveCount(0);

    // Filter to accepted — only accepted entries
    const statusSelect = page.getByRole("combobox", { name: "Status" });
    await statusSelect.selectOption("revoked");

    await expect(page.getByText("Re-acceptance required after changes")).toBeVisible();
    await expect(
      page.getByText("Matching filters").locator("..").getByText("1", { exact: true }),
    ).toBeVisible();
    // Clear filters button appears
    await expect(page.getByRole("button", { name: "Clear filters" })).toBeVisible();

    diagnostics.assertClean();
  });

  test("Owner acceptance mobile active tab is visible", async ({ page }) => {
    const diagnostics = monitorBrowser(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    const activeLink = page.getByRole("link", {
      name: "Owner product review",
      exact: true,
    });
    await expect(activeLink).toBeVisible();
    await expect(activeLink).toHaveAttribute("aria-current", "page");

    diagnostics.assertClean();
  });

  test("Owner acceptance mixed ledger fixture includes stale and unavailable entries", async ({
    page,
  }) => {
    const diagnostics = monitorBrowser(page);

    await page.goto("/ui/engineering/owner-acceptance?fixture=products");

    await expect(page.getByText("Re-evaluation required; binding has changed")).toBeVisible();
    await expect(page.getByText("Evidence unavailable; retry after prerequisites are met")).toBeVisible();

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
