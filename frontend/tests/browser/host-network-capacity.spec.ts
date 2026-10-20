import { expect, test } from "./state";
import type { HostNetworkCapacityObservation } from "../../src/generated/openapi.ts";

test("placement shows host capacity and refreshes short, denied and unavailable evidence", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products?fixture=products");
  const { product, detail, identity, incidents } = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(path) as typeof import("../../src/dev-fixtures");
    const product = fixtures.productsForFixture("products")[0];
    return { product, detail: fixtures.environmentForFixture("products", product.product, "testing")!,
      identity: fixtures.fixtureIdentity, incidents: fixtures.incidentsForFixture("products", product.product, "testing") };
  });
  let observation: HostNetworkCapacityObservation = {
    server_id: "selected-host", observed_at: new Date().toISOString(), status: "available",
    capacity: { total_slots: 256, free_slots: 200 }, required_networks: 1, safety_margin: 2, reason: "",
  };
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [product] } }));
  await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product } }));
  await page.route(`**/v1/products/${product.product}/environments/testing*`, route => {
    const withCapacity = new URL(route.request().url()).searchParams.has("include_host_network_capacity");
    return route.fulfill({ json: { status: "ok", trace_id: "environment", environment: detail,
      host_network_capacity_available: true, host_network_capacity: withCapacity ? observation : null } });
  });
  await page.route(`**/v1/products/${product.product}/environments/testing/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", trace_id: "incidents", incident_list: incidents } }));
  await page.goto(`/ui/products/${product.product}/environments/testing`);
  const capacity = page.locator(".host-network-capacity");
  await expect(capacity).toContainText("200 free of 256 bridge networks");
  await expect(capacity).toContainText("selected-host");
  observation = { ...observation, status: "short", capacity: { total_slots: 256, free_slots: 2 }, reason: "The selected host has insufficient free bridge networks for this create and its safety margin." };
  await capacity.getByRole("button", { name: "Refresh host capacity" }).click();
  await expect(capacity).toHaveAttribute("data-status", "short");
  await expect(capacity).toContainText("2 free of 256 bridge networks");
  await expect(capacity).toContainText(observation.reason);
  await capacity.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `../tmp/browser-smoke/host-network-capacity-short-${testInfo.project.name}.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  for (const status of ["denied", "unavailable"] as const) {
    observation = { ...observation, status, capacity: null, reason: status === "denied" ? "The provider denied the capacity read." : "The host capacity could not be verified." };
    await capacity.getByRole("button", { name: "Refresh host capacity" }).click();
    await expect(capacity).toHaveAttribute("data-status", status);
    await expect(capacity).toContainText(observation.reason);
    await expect(capacity.getByText(/free of/)).toHaveCount(0);
  }
});
