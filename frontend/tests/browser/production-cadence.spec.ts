import { expect, setClockTime, test, waitForState } from "./state";
import { DEADLINE_RETRY_INTERVAL_MS, READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

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
      // Exercise the evidence reader without unrelated administrator forms.
      // Their draft preservation remains covered by open-lane-refresh.spec.ts.
      return { product, detail, identity: { ...module.fixtureIdentity, role: "read_only" as const },
        incidents: module.incidentsForFixture("products", product.product, "testing") };
    });
    // The service updates at its evidence deadline, independently of when the
    // browser opened. The first response has only a small fresh window left.
    // Independent fixture input: production observations have a one-minute
    // lifetime, and this monitor publishes after the slot starts.
    const cadence = 60_000;
    const publicationDelay = 11_000;
    const origin = Date.now();
    let serviceTime = origin + cadence * 0.9;
    let browserTime = serviceTime;
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
      const observed = origin + Math.floor(((stoppedAt ?? serviceTime) - origin - publicationDelay) / cadence) * cadence;
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
      serviceTime = browserTime;
      return route.fulfill({ json: { status: "ok", products: [response(fixture.product)] } });
    });
    await page.route(target, async route => {
      reads++;
      const name = lastName = `Cadence observation ${reads}`;
      if (unavailable) return route.fulfill({ status: 503, json: { error: { code: "read_unavailable", message: "Cadence fixture unavailable" } } });
      serviceTime = browserTime;
      const payload = view === "environment"
        ? { environment: { ...response(fixture.detail), display_name: name } }
        : view === "workspace"
          ? { product: { ...response(fixture.product), display_name: name } }
          : { products: [{ ...response(fixture.product), display_name: name }] };
      return route.fulfill({ json: { status: "ok", ...payload } });
    });
    await page.route(`${detailUrl}/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", incident_list: fixture.incidents } }));
    await page.goto(`/ui/products${view === "index" ? "" : `/${fixture.product.product}${view === "environment" ? "/environments/prod" : ""}`}`);
    const main = page.getByRole("main");
    const findSignal = (root: typeof main) => view === "index" ? root.locator('.directory-lane[data-lane="prod"]')
      : root.locator(view === "workspace" ? ".signal-tile" : ".condition-tile").filter({ hasText: view === "workspace" ? /^Production/i : /^Runtime identity/i });
    const signal = findSignal(main);
    const attribute = view === "index" ? "data-operational" : "data-tone";
    const healthy = view === "environment" ? "pass" : "verified";
    await expect(signal).toHaveAttribute(attribute, healthy);
    await expect(page.getByText(lastName, { exact: true }).last()).toBeVisible();
    const advanceRead = async (milliseconds: number) => {
      // Both clocks use the phase's explicit wall time. The publisher still
      // follows its independent minute cadence and delayed publication. No
      // extra browser RPC is needed inside each intercepted response.
      browserTime += milliseconds;
      await page.clock.runFor(milliseconds);
    };
    const advance = async (milliseconds: number, tone: string) => {
      const previousName = lastName;
      await advanceRead(milliseconds);
      // Check response replacement and its freshness in one browser assertion.
      // Retained loading data cannot match because it still has the old name.
      const previousObservation = page.locator(view === "index" ? ".directory-product strong" : "h1")
        .filter({ hasText: new RegExp(`^${previousName}$`) });
      const nextSurface = main.filter({ hasNot: previousObservation });
      await expect(findSignal(nextSurface)).toHaveAttribute(attribute, tone);
    };
    for (let minute = 0; minute < 31; minute++) {
      // Observe each boundary before publication, then its recovery. A single
      // minute-sized clock jump would answer the pending read too late and
      // hide the publication race for the rest of this long-lived page.
      await advance(origin + (minute + 1) * cadence + 1 - browserTime, "warning");
      await advance(publicationDelay + DEADLINE_RETRY_INTERVAL_MS, healthy);
    }
    await page.screenshot({ path: `../tmp/browser-smoke/production-cadence-${view}-${testInfo.project.name}.png`, fullPage: true });
    // Successful GETs must not renew stopped monitor evidence.
    stoppedAt = serviceTime;
    await advance(cadence, "warning");
    // Repeated successful reads cannot renew the stopped observation, and the
    // short follow-up ends after one cadence rather than spinning forever.
    await advance(READ_REFRESH_INTERVAL_MS, "warning");
    const stoppedReads = reads;
    browserTime += DEADLINE_RETRY_INTERVAL_MS * 2;
    await page.clock.runFor(DEADLINE_RETRY_INTERVAL_MS * 2);
    expect(reads).toBe(stoppedReads);
    unavailable = true;
    await advanceRead(READ_REFRESH_INTERVAL_MS);
    await expect(page.getByText("Cadence fixture unavailable", { exact: false }).first()).toBeVisible();
    await expect(signal).toHaveAttribute(attribute, "warning");
    expect(mutations).toEqual([]);
  });
}

for (const heldRead of [false, true]) {
test(`staggered lane deadlines recover independently${heldRead ? " across a pending read" : ""}`, async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const fixture = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath);
    const product = structuredClone(module.productsForFixture("products")[0]);
    const lane = product.environments.find(lane => lane.environment === "testing")!;
    lane.warnings = [];
    lane.topology.warnings = [];
    lane.health_monitoring.open_incidents = [];
    lane.health_monitoring.checks = [lane.health_monitoring.checks[0]];
    Object.assign(lane.health_monitoring.checks[0], { status: "pass", probe_effective: true, incident_status: "" });
    product.environments = [lane, { ...structuredClone(lane), environment: "prod" }];
    return { product, identity: module.fixtureIdentity };
  });
  const start = Date.now();
  await setClockTime(page, start);
  let reads = 0;
  let lastName = "";
  let holdRead = false;
  let releaseRead: (() => void) | undefined;
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixture.identity } }));
  await page.route("**/v1/products", async route => {
    const name = lastName = `Staggered observation ${++reads}`;
    if (holdRead) await new Promise<void>(resolve => { releaseRead = resolve; });
    const now = await page.evaluate(() => Date.now());
    const product = structuredClone(fixture.product);
    product.display_name = name;
    for (const lane of product.environments) {
      // Production's proof recovers first; testing was already due during its
      // retry window but publishes later. These are independent service inputs.
      const testing = lane.environment === "testing";
      const deadline = start + (testing ? 7_000 : 5_000);
      const published = now >= start + (testing ? 30_000 : 15_000);
      const expires = deadline + (published ? testing ? 300_000 : 60_000 : 0);
      const stamp = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) {
          record.stale_after = new Date(expires).toISOString();
          record.freshness_status = expires >= now ? "verified" : "stale";
        }
        if ("trust_state" in record) record.trust_state = expires >= now ? "verified" : "stale";
        Object.values(record).forEach(stamp);
      };
      stamp(lane);
    }
    return route.fulfill({ json: { status: "ok", products: [product] } });
  });
  await page.goto("/ui/products");
  const prod = page.locator('.directory-lane[data-lane="prod"]');
  const testing = page.locator('.directory-lane[data-lane="testing"]');
  await expect(prod).toHaveAttribute("data-operational", "verified");
  await expect(testing).toHaveAttribute("data-operational", "verified");
  const advance = async (milliseconds: number) => {
    const before = reads;
    await page.clock.runFor(milliseconds);
    await waitForState(() => reads).toBeGreaterThan(before);
    await expect(page.getByText(lastName, { exact: true }).last()).toBeVisible();
  };
  holdRead = heldRead;
  if (heldRead) {
    await page.clock.runFor(5_001);
    await waitForState(() => Boolean(releaseRead)).toBe(true);
    await expect(prod).toHaveAttribute("data-operational", "warning");
    await expect(testing).toHaveAttribute("data-operational", "verified");
    await page.clock.runFor(2_000);
    await expect(testing).toHaveAttribute("data-operational", "warning");
    holdRead = false;
    releaseRead!();
    await expect(page.getByText(lastName, { exact: true }).last()).toBeVisible();
  } else {
    await advance(5_001);
    await expect(prod).toHaveAttribute("data-operational", "warning");
    await expect(testing).toHaveAttribute("data-operational", "verified");
    await advance(2_000);
    await expect(testing).toHaveAttribute("data-operational", "warning");
  }
  await advance(10_000);
  await expect(prod).toHaveAttribute("data-operational", "verified");
  await expect(testing).toHaveAttribute("data-operational", "warning");
  await advance(30_000 + DEADLINE_RETRY_INTERVAL_MS - 17_001);
  await expect(testing).toHaveAttribute("data-operational", "verified");
});
}
