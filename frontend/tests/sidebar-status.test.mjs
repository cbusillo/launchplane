import assert from "node:assert/strict";
import { test } from "node:test";
import { sidebarStatus } from "../src/sidebar-status.ts";
import { environmentOperationalTone } from "../src/product-environment-signal.ts";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { search: "" } } });
const { productsForFixture } = await import("../src/dev-fixtures.ts");
if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
else Reflect.deleteProperty(globalThis, "window");

test("sidebar preserves operational tone and reports every recorded incident layer", () => {
  const lane = structuredClone(productsForFixture("products")[0].environments[0]);
  lane.health_monitoring.open_incidents = [
    { failure_layer: "dns" }, { failure_layer: "tls" }, { failure_layer: "runtime_identity" },
  ];
  const status = sidebarStatus(lane, "Testing");
  assert.equal(status.tone, environmentOperationalTone(lane));
  assert.match(status.description, /DNS/);
  assert.match(status.description, /certificate/i);
  assert.match(status.description, /identity/i);
});

test("effective failing checks supply diagnostics without inventing a proxy or app cause", () => {
  const lane = structuredClone(productsForFixture("products")[0].environments[0]);
  lane.health_monitoring.open_incidents = [];
  lane.topology.observed.tls_domains = [];
  lane.topology.observed.placement.runtime_identity_status = "match";
  const check = lane.health_monitoring.checks[0];
  check.probe_effective = true;
  check.status = "fail";
  for (const [code, layer] of [["dns_failure", /DNS/], ["tls_hostname_mismatch", /certificate/i], ["wrong_runtime_identity", /identity/i]]) {
    check.failure_code = code;
    assert.match(sidebarStatus(lane, "Production").description, layer);
  }
  check.failure_code = "http_error";
  assert.doesNotMatch(sidebarStatus(lane, "Production").description, /proxy|app/i);
  check.failure_code = "dns_failure";
  check.probe_effective = false;
  assert.doesNotMatch(sidebarStatus(lane, "Production").description, /DNS/);
});
