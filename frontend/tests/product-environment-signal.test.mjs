import assert from "node:assert/strict";
import { test } from "node:test";
import { environmentEvidenceDeadlines, environmentReleaseHealthy, environmentOperationalTone, laneSignal, operationalAttentionReasons, operationalWarningReasons } from "../src/product-environment-signal.ts";

/** @type {import("../src/generated/openapi.ts").ProductLaneSignal} */
const current = { tone: "verified", sidebar_tone: "verified", description: "Healthy", reasons: [], public_observation_gap: null, attention_reasons: [], warning_reasons: [], release_healthy: true };
/** @type {import("../src/generated/openapi.ts").ProductLaneSignal} */
const expired = { tone: "warning", sidebar_tone: "stale", description: "Evidence expired", reasons: [], public_observation_gap: null, attention_reasons: [], warning_reasons: [], release_healthy: false };

test("all signal consumers read the service result", () => {
  /** @type {import("../src/generated/openapi.ts").ProductLaneSignal} */
  const supplied = { ...current, tone: "danger", attention_reasons: ["Failed check"], warning_reasons: ["Recovery warning"] };
  const lane = { lane_status: { evaluated_at: new Date().toISOString(), current: supplied, transitions: [] } };
  assert.equal(environmentOperationalTone(lane), lane.lane_status.current.tone);
  assert.deepEqual(operationalAttentionReasons(lane), lane.lane_status.current.attention_reasons);
  assert.deepEqual(operationalWarningReasons(lane), lane.lane_status.current.warning_reasons);
  assert.equal(environmentReleaseHealthy(lane), lane.lane_status.current.release_healthy);
});

test("an open page follows service-computed expiry transitions without changing source evidence", () => {
  const deadline = Date.now();
  const lane = { lane_status: { evaluated_at: new Date().toISOString(), current, transitions: [{ after: new Date(deadline).toISOString(), signal: expired }] } };
  assert.equal(laneSignal(lane, deadline), current);
  assert.equal(laneSignal(lane, deadline + 1), expired);
  assert.equal(environmentReleaseHealthy(lane, deadline + 1), false);
  assert.equal(lane.lane_status.current, current);
});

test("missing service status is unknown and never release healthy", () => {
  for (const lane of [null, {}, { lane_status: null }]) {
    assert.equal(environmentOperationalTone(lane), "missing");
    assert.equal(environmentReleaseHealthy(lane), false);
  }
});

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { search: "" } } });
const { productsForFixture } = await import("../src/dev-fixtures.ts");
if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
else Reflect.deleteProperty(globalThis, "window");

test("service transitions schedule expiry even when advisory TLS is outside badge deadlines", () => {
  /** @type {import("../src/generated/openapi.ts").ProductSiteOverview} */
  const product = productsForFixture("products")[0];
  const lane = structuredClone(product.environments[0]);
  const transition = { after: new Date().toISOString(), signal: expired };
  lane.lane_status = { evaluated_at: new Date().toISOString(), current, transitions: [transition] };
  lane.topology.observed.tls_domains.forEach(domain => { domain.advisory = true; });
  assert.ok(environmentEvidenceDeadlines(lane).includes(transition.after));
});
