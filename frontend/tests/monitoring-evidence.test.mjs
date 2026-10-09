import assert from "node:assert/strict";
import { test } from "node:test";
import { monitoringEvidenceTrust } from "../src/monitoring-evidence.ts";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { search: "" } } });
const { productsForFixture } = await import("../src/dev-fixtures.ts");
if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
else Reflect.deleteProperty(globalThis, "window");

function check() {
  const check = structuredClone(productsForFixture("products")[0].environments[0].health_monitoring.checks[0]);
  check.trust_state = "verified";
  check.provenance.stale_after = new Date(Date.now() + 60_000).toISOString();
  return check;
}

test("effective observation evidence owns monitoring completeness", () => {
  const required = check();
  assert.equal(monitoringEvidenceTrust([required]), "verified");
  required.trust_state = "missing";
  assert.equal(monitoringEvidenceTrust([required]), "missing");
  required.trust_state = "verified";
  const deadline = Date.parse(required.provenance.stale_after);
  assert.equal(monitoringEvidenceTrust([required], deadline + 1), "stale");
  required.trust_state = "unsupported";
  assert.equal(monitoringEvidenceTrust([required]), "unsupported");
});

test("one absent observation prevents an otherwise complete set from hiding it", () => {
  const missing = check();
  missing.trust_state = "missing";
  assert.equal(monitoringEvidenceTrust([check(), missing]), "missing");
});

test("disabled and inapplicable probes do not create false incompleteness", () => {
  const inactive = check();
  inactive.probe_effective = false;
  inactive.trust_state = "missing";
  assert.equal(monitoringEvidenceTrust([check(), inactive]), "verified");
  assert.equal(monitoringEvidenceTrust([inactive]), "recorded");
});
