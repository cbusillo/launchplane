import { expect, setClockTime, test, waitForState } from "./state";
import { READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

for (const view of ["index", "workspace", "environment"] as const) {
  test(`open ${view} follows production evidence deadlines for over thirty minutes`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixture = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const product = structuredClone(module.productsForFixture("products")[0]);
      const lane = structuredClone(product.environments.find(lane => lane.environment === "testing")!);
      lane.environment = "prod";
      lane.warnings = [];
      lane.topology.warnings = [];
      lane.health_monitoring.open_incidents = [];
      lane.health_monitoring.checks = [lane.health_monitoring.checks[0]];
      Object.assign(lane.health_monitoring.checks[0], { status: "pass", probe_effective: true, incident_status: "" });
      product.environments = [lane];
      const detail = module.environmentForFixture("products", product.product, "testing");
      Object.assign(detail, lane);
      return { product, detail, identity: module.fixtureIdentity,
        incidents: module.incidentsForFixture("products", product.product, "testing") };
    });
    // The service updates at its evidence deadline, independently of when the
    // browser opened. The first response has only a small fresh window left.
    const cadence = READ_REFRESH_INTERVAL_MS;
    const origin = Date.now();
    let serviceTime = origin + cadence * 0.9;
    let stoppedAt: number | undefined;
    let unavailable = false;
    let reads = 0;
    let lastName = "";
    const mutations: string[] = [];
    await setClockTime(page, serviceTime);
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    const response = <T,>(source: T): T => {
      const copy = structuredClone(source);
      const observed = origin + Math.floor(((stoppedAt ?? serviceTime) - origin) / cadence) * cadence;
      const fresh = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) {
          record.recorded_at = record.refreshed_at = new Date(observed).toISOString();
          record.stale_after = new Date(observed + cadence).toISOString();
          record.freshness_status = serviceTime <= observed + cadence ? "verified" : "stale";
        }
        if ("trust_state" in record) record.trust_state = serviceTime <= observed + cadence ? "verified" : "stale";
        Object.values(record).forEach(fresh);
      };
      fresh(copy);
      return copy;
    };
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: "Unused fixture read" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixture.identity, csrf_token: "fixture-csrf" } }));
    const productUrl = `**/v1/products/${fixture.product.product}`;
    const detailUrl = `${productUrl}/environments/prod`;
    const target = view === "index" ? "**/v1/products" : view === "workspace" ? productUrl : detailUrl;
    if (view !== "index") await page.route("**/v1/products", async route => {
      serviceTime = await page.evaluate(() => Date.now());
      return route.fulfill({ json: { status: "ok", products: [response(fixture.product)] } });
    });
    await page.route(target, async route => {
      reads++;
      if (unavailable) return route.fulfill({ status: 503, json: { error: { code: "read_unavailable", message: "Cadence fixture unavailable" } } });
      serviceTime = await page.evaluate(() => Date.now());
      lastName = `Cadence observation ${reads}`;
      const product = { ...response(fixture.product), display_name: lastName };
      const detail = { ...response(fixture.detail), display_name: lastName };
      return route.fulfill({ json: { status: "ok", products: [product], product, environment: detail } });
    });
    await page.route(`${detailUrl}/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", incident_list: fixture.incidents } }));
    await page.goto(`/ui/products${view === "index" ? "" : `/${fixture.product.product}${view === "environment" ? "/environments/prod" : ""}`}`);
    const signal = view === "index" ? page.locator('.directory-lane[data-lane="prod"]')
      : page.locator(view === "workspace" ? ".signal-tile" : ".condition-tile").filter({ hasText: view === "workspace" ? /^Production/i : /^Runtime identity/i });
    const attribute = view === "index" ? "data-operational" : "data-tone";
    const healthy = view === "environment" ? "pass" : "verified";
    await expect(signal).toHaveAttribute(attribute, healthy);
    for (let minute = 0; minute <= 30; minute++) {
      const before = reads;
      const advance = minute === 0 ? cadence * 0.2 : cadence;
      await page.clock.runFor(advance);
      await waitForState(() => reads).toBeGreaterThan(before);
      await expect(page.getByText(lastName, { exact: true }).last()).toBeVisible();
      await expect(signal).toHaveAttribute(attribute, healthy);
    }
    await page.screenshot({ path: `../tmp/browser-smoke/production-cadence-${view}-${testInfo.project.name}.png`, fullPage: true });
    // Successful GETs must not renew stopped monitor evidence.
    stoppedAt = serviceTime;
    await page.clock.runFor(cadence);
    await expect(signal).toHaveAttribute(attribute, "warning");
    await expect(signal).toContainText("Stale");
    unavailable = true;
    await page.clock.runFor(cadence);
    await expect(page.getByText("Cadence fixture unavailable", { exact: false }).first()).toBeVisible();
    await expect(signal).toHaveAttribute(attribute, "warning");
    expect(mutations).toEqual([]);
  });
}
