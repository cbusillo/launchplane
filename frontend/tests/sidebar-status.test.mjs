import assert from "node:assert/strict";
import { test } from "node:test";
import { sidebarLayerLabel, sidebarStatus } from "../src/sidebar-status.ts";
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
  for (const layer of ["dns", "tls", "runtime_identity"]) {
    assert.ok(status.description.includes(sidebarLayerLabel(layer)));
  }
});

test("effective failing checks supply diagnostics without inventing a proxy or app cause", () => {
  const lane = structuredClone(productsForFixture("products")[0].environments[0]);
  lane.health_monitoring.open_incidents = [];
  lane.topology.observed.tls_domains = [];
  lane.topology.observed.placement.runtime_identity_status = "match";
  const check = lane.health_monitoring.checks[0];
  check.probe_effective = true;
  check.status = "fail";
  for (const [code, layer] of [["dns_failure", "dns"], ["tls_hostname_mismatch", "tls"], ["wrong_runtime_identity", "runtime_identity"]]) {
    check.failure_code = code;
    assert.ok(sidebarStatus(lane, "Production").description.includes(sidebarLayerLabel(layer)));
  }
  check.failure_code = "http_error";
  assert.doesNotMatch(sidebarStatus(lane, "Production").description, /\b(proxy|app)\b/i);
  check.failure_code = "dns_failure";
  check.probe_effective = false;
  assert.ok(!sidebarStatus(lane, "Production").description.includes(sidebarLayerLabel("dns")));
});

test("an expiring certificate is not named as a failure layer", () => {
  const lane = structuredClone(productsForFixture("products")[0].environments[0]);
  lane.trust_state = "recorded";
  lane.provenance.freshness_status = "verified";
  lane.provenance.stale_after = new Date(Date.now() + 60_000).toISOString();
  const freshen = value => {
    if (!value || typeof value !== "object") return;
    if ("stale_after" in value) value.stale_after = lane.provenance.stale_after;
    Object.values(value).forEach(freshen);
  };
  freshen(lane.topology);
  lane.warnings = [];
  lane.topology.warnings = [];
  lane.topology.observed.placement.runtime_identity_status = "match";
  lane.health_monitoring.open_incidents = [];
  lane.health_monitoring.checks = [lane.health_monitoring.checks[0]];
  Object.assign(lane.health_monitoring.checks[0], { probe_effective: true, status: "pass", trust_state: "verified", incident_status: "" });
  lane.health_monitoring.checks[0].provenance.stale_after = lane.provenance.stale_after;
  lane.topology.observed.tls_domains = [{ status: "expiring", trust_state: "verified", provenance: lane.provenance }];
  assert.notEqual(environmentOperationalTone(lane), "danger");
  assert.ok(!sidebarStatus(lane, "Production").description.includes(sidebarLayerLabel("tls")));
  Object.assign(lane.health_monitoring.checks[0], { status: "fail", failure_code: "dns_failure" });
  assert.equal(environmentOperationalTone(lane), "danger");
  assert.ok(sidebarStatus(lane, "Production").description.includes(sidebarLayerLabel("dns")));
  assert.ok(!sidebarStatus(lane, "Production").description.includes(sidebarLayerLabel("tls")));
});

test("stale inventory replaces cached failures and absent lanes with clocks", () => {
  const lane = structuredClone(productsForFixture("products")[0].environments[0]);
  lane.health_monitoring.open_incidents = [{ failure_layer: "dns" }];
  assert.equal(sidebarStatus(lane, "Testing").tone, "danger");
  for (const cached of [lane, null]) {
    const status = sidebarStatus(cached, "Testing", "Stale data; last refresh failed");
    assert.equal(status.tone, "stale");
    assert.doesNotMatch(status.description, /Alert|Warning|DNS/);
  }
});
