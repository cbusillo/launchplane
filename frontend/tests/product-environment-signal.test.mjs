import assert from "node:assert/strict";
import { test } from "node:test";
import { environmentOperationalTone, expireProductEvidence } from "../src/product-environment-signal.ts";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { search: "" } } });
const { productsForFixture } = await import("../src/dev-fixtures.ts");
if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
else Reflect.deleteProperty(globalThis, "window");

function environment() {
  const lane = structuredClone(productsForFixture("products")[0].environments.find(lane => lane.environment === "testing"));
  lane.trust_state = "recorded";
  lane.provenance.freshness_status = "verified";
  lane.provenance.stale_after = new Date(Date.now() + 60_000).toISOString();
  lane.warnings = [];
  lane.topology.warnings = [];
  lane.topology.observed.tls_domains = [];
  lane.topology.observed.placement.runtime_identity_status = "unchecked";
  lane.health_monitoring.checks = [structuredClone(lane.health_monitoring.checks[0])];
  const check = lane.health_monitoring.checks[0];
  check.probe_effective = true;
  check.status = "pass";
  check.trust_state = "verified";
  check.incident_status = "";
  check.provenance.stale_after = lane.provenance.stale_after;
  return lane;
}

test("a fresh identity and health verification makes a monitored lane green", () =>
  assert.equal(environmentOperationalTone(environment()), "verified"));

test("informational topology stays visible without blocking current verification", () => {
  const lane = environment();
  lane.topology.warnings = [{ code: "external_ingress_internals_unsupported", severity: "info", scope: "ingress", detail: "External proxy internals are unavailable.", domain_name: "" }];
  assert.equal(environmentOperationalTone(lane), "verified");
  lane.health_monitoring.checks[0].status = "fail";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.checks[0].status = "pass";
  lane.health_monitoring.checks[0].incident_status = "open";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.checks[0].incident_status = "";
  lane.provenance.freshness_status = "stale";
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("no website never bypasses health, identity, placement or applicable topology gates", () => {
  const lane = environment();
  lane.topology.desired.public_website = "none";
  lane.topology.warnings = [{ code: "public_website_not_applicable", severity: "info", scope: "authority", detail: "No public website declared.", domain_name: "" }];
  assert.equal(environmentOperationalTone(lane), "verified");
  for (const state of ["missing", "unsupported"]) {
    lane.trust_state = state;
    assert.notEqual(environmentOperationalTone(lane), "verified");
  }
  lane.trust_state = "recorded";
  lane.health_monitoring.checks[0].probe_effective = false;
  assert.notEqual(environmentOperationalTone(lane), "verified");
  lane.health_monitoring.checks[0].probe_effective = true;
  lane.topology.observed.placement.runtime_identity_status = "mismatch";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.topology.observed.placement.runtime_identity_status = "match";
  lane.topology.warnings.push({ code: "stale_route_authority", severity: "error", scope: "authority", detail: "Stale authority", domain_name: "" });
  assert.equal(environmentOperationalTone(lane), "danger");
});

test("negative TLS remains blocking beside informational evidence", () => {
  const lane = environment();
  lane.topology.warnings = [{ code: "external_ingress_internals_unsupported", severity: "info", scope: "ingress", detail: "External proxy internals unavailable", domain_name: "" }];
  for (const status of ["expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable"]) {
    lane.topology.observed.tls_domains = [{ status }];
    assert.equal(environmentOperationalTone(lane), "danger");
  }
});

test("stale or unverified identity cannot show green with passing HTTP", () => {
  /** @type {import("../src/generated/openapi.ts").DataProvenance["freshness_status"][]} */
  const statuses = ["stale", "recorded", "missing"];
  for (const freshness_status of statuses) {
    const lane = environment();
    lane.provenance.freshness_status = freshness_status;
    assert.notEqual(environmentOperationalTone(lane), "verified");
  }
});

test("health failure or a mismatch incident turns the lane red", () => {
  for (const check of [{ status: "fail" }, { incident_status: "open" }]) {
    const lane = environment();
    Object.assign(lane.health_monitoring.checks[0], check);
    assert.equal(environmentOperationalTone(lane), "danger");
  }
});

test("every effective check and topology warning still matters", () => {
  const lane = environment();
  const check = structuredClone(lane.health_monitoring.checks[0]);
  check.status = "missing";
  check.trust_state = "missing";
  lane.health_monitoring.checks.push(check);
  assert.equal(environmentOperationalTone(lane), "warning");
  lane.health_monitoring.checks.pop();
  const product = productsForFixture("products")[0];
  const warning = structuredClone(product.environments.flatMap(environment => environment.topology.warnings)[0]);
  warning.severity = "warning";
  lane.topology.warnings.push(warning);
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("an open page cannot keep an expired verification green", () => {
  const lane = environment();
  const now = Date.now();
  lane.provenance.stale_after = new Date(now).toISOString();
  assert.equal(environmentOperationalTone(lane, now), "verified");
  assert.equal(environmentOperationalTone(lane, now + 1), "warning");
});

test("a recorded runtime mismatch stays red without an effective monitor check", () => {
  const lane = environment();
  lane.health_monitoring.checks = [];
  lane.topology.observed.placement.runtime_identity_status = "mismatch";
  assert.equal(environmentOperationalTone(lane), "danger");
});

test("advisory missing runtime identity is unverified rather than a failure", () => {
  const lane = environment();
  lane.provenance.freshness_status = "recorded";
  lane.topology.observed.placement.runtime_identity_status = "missing";
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("product and lane badges expire together with the signal", () => {
  const lane = environment();
  const product = structuredClone(productsForFixture("products")[0]);
  product.environments = [lane];
  const expired = expireProductEvidence(product, Date.parse(lane.provenance.stale_after) + 1);
  assert.equal(expired.trust_state, "stale");
  assert.equal(expired.environments[0].trust_state, "stale");
  assert.equal(environmentOperationalTone(expired.environments[0]), "warning");
});
