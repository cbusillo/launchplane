import assert from "node:assert/strict";
import { test } from "node:test";
import { environmentOperationalTone } from "../src/product-environment-signal.ts";

function environment() {
  return {
    trust_state: "recorded", provenance: { freshness_status: "verified" }, warnings: [],
    topology: { warnings: [], observed: { tls_domains: [] } },
    health_monitoring: { checks: [{ probe_effective: true, status: "pass", trust_state: "verified", incident_status: "" }] },
  };
}

test("a fresh identity and health verification makes a monitored lane green", () => {
  assert.equal(environmentOperationalTone(environment()), "verified");
});

test("stale or unverified identity cannot show green with passing HTTP", () => {
  for (const freshness_status of ["stale", "recorded", "missing"]) {
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
  lane.health_monitoring.checks.push({ probe_effective: true, status: "missing", trust_state: "missing" });
  assert.equal(environmentOperationalTone(lane), "warning");
  lane.health_monitoring.checks.pop();
  lane.topology.warnings.push({ severity: "warning" });
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
  lane.topology.observed.placement = { runtime_identity_status: "mismatch" };
  assert.equal(environmentOperationalTone(lane), "danger");
});

test("advisory missing runtime identity is unverified rather than a failure", () => {
  const lane = environment();
  lane.provenance.freshness_status = "recorded";
  lane.topology.observed.placement = { runtime_identity_status: "missing" };
  assert.equal(environmentOperationalTone(lane), "warning");
});
