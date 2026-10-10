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

test("a canonical generated occurrence blocks green independently of declared checks", () => {
  const lane = environment();
  const incident = structuredClone(productsForFixture("products")[0].environments.find(lane => lane.environment === "prod").health_monitoring.open_incidents[0]);
  incident.check_name = "launchplane-deploy-fence";
  incident.check_kind = "provider";
  lane.health_monitoring.open_incidents = [incident];
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.open_incidents = [];
  assert.equal(environmentOperationalTone(lane), "verified");
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
