import { expect, setClockTime, test } from "./state";

test("a red lane states its cause in the directory and the workspace", { tag: "@responsive" }, async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const fixtures = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    const product = structuredClone(module.productsForFixture("products")[0]);
    const lane = product.environments.find(lane => lane.environment === "testing")!;
    const now = Date.now();
    lane.trust_state = "recorded";
    lane.provenance.freshness_status = "verified";
    lane.provenance.stale_after = new Date(now + 60_000).toISOString();
    const freshen = (value: unknown) => {
      if (!value || typeof value !== "object") return;
      const record = value as Record<string, unknown>;
      if ("stale_after" in record) record.stale_after = lane.provenance.stale_after;
      Object.values(record).forEach(freshen);
    };
    freshen(lane.topology);
    lane.warnings = [];
    lane.topology.warnings = [];
    lane.topology.observed.placement.runtime_identity_status = "match";
    lane.health_monitoring.checks = [structuredClone(lane.health_monitoring.checks[0])];
    const check = lane.health_monitoring.checks[0];
    Object.assign(check, { probe_effective: true, status: "pass", trust_state: "verified", incident_status: "" });
    check.provenance.stale_after = lane.provenance.stale_after;
    // The health check passes, yet an open delivery incident keeps the lane red.
    const incident = structuredClone(product.environments.find(lane => lane.environment === "prod")!.health_monitoring.open_incidents[0]);
    Object.assign(incident, { environment: "testing", severity: "warning", failure_code: "testing_delivery_failed", check_kind: "provider" });
    lane.health_monitoring.open_incidents = [incident];
    return { product, identity: module.fixtureIdentity, now, passSummary: lane.public_ingress.summary };
  });
  await setClockTime(page, fixtures.now);
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [fixtures.product] } }));
  await page.route(`**/v1/products/${fixtures.product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
  await page.goto("/ui/products");

  const cause = /warning incident open: testing delivery failed/i;
  const directoryLane = page.locator('.directory-lane[data-lane="testing"]');
  await expect(directoryLane).toHaveAttribute("data-operational", "danger");
  await expect(directoryLane.locator(":scope > strong")).not.toContainText(fixtures.passSummary);
  const directoryCause = directoryLane.locator(".directory-attention");
  await expect(directoryCause).toHaveText(cause);
  // The cause wraps rather than being cut off by the single-line headline.
  expect(await directoryCause.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);

  await page.getByRole("list", { name: "Launchplane products" })
    .getByRole("link", { name: new RegExp(fixtures.product.display_name) }).click();
  const workspaceLane = page.locator('.environment-row[data-lane="testing"]');
  await expect(workspaceLane.locator(".environment-attention")).toHaveText(cause);
  await expect(workspaceLane.locator(".environment-facts")).not.toContainText(fixtures.passSummary);
});

test("a red private lane never headlines a retained passing public observation", async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const fixtures = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    const product = structuredClone(module.productsForFixture("products")[0]);
    const lane = product.environments.find(lane => lane.environment === "testing")!;
    lane.topology.desired.public_website = "none";
    lane.public_ingress.observed_at = new Date().toISOString();
    lane.public_ingress.summary = "Public ingress was reachable when last observed.";
    const check = lane.health_monitoring.checks[0];
    Object.assign(check, { probe_effective: true, kind: "private_http", status: "fail", summary: "Private health check failed: connection refused." });
    return { product, identity: module.fixtureIdentity, retained: lane.public_ingress.summary, cause: check.summary };
  });
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [fixtures.product] } }));
  await page.route(`**/v1/products/${fixtures.product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
  await page.goto("/ui/products");
  const directoryLane = page.locator('.directory-lane[data-lane="testing"]');
  await expect(directoryLane).toHaveAttribute("data-operational", "danger");
  await expect(directoryLane.locator(":scope > strong")).not.toContainText(fixtures.retained);
  await expect(directoryLane.locator(".directory-attention")).toContainText(fixtures.cause);
  await page.getByRole("list", { name: "Launchplane products" })
    .getByRole("link", { name: new RegExp(fixtures.product.display_name) }).click();
  const workspaceLane = page.locator('.environment-row[data-lane="testing"]');
  await expect(workspaceLane.locator(".environment-attention")).toContainText(fixtures.cause);
  // The retained observation stays visible, labelled as history rather than current health.
  await expect(workspaceLane).toContainText(`Recorded public observation: ${fixtures.retained}`);
});
