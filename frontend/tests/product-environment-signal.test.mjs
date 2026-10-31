import assert from "node:assert/strict";
import { test } from "node:test";
import { environmentReleaseHealthy, environmentOperationalTone, laneSignal, operationalAttentionReasons, operationalWarningReasons } from "../src/product-environment-signal.ts";

const current = { tone: "verified", sidebar_tone: "verified", description: "Healthy", attention_reasons: [], warning_reasons: [], release_healthy: true };
const expired = { tone: "warning", sidebar_tone: "stale", description: "Evidence expired", attention_reasons: [], warning_reasons: [], release_healthy: false };

test("all signal consumers read the service result", () => {
  const lane = { lane_status: { current: { ...current, tone: "danger", attention_reasons: ["Failed check"], warning_reasons: ["Recovery warning"] }, transitions: [] } };
  assert.equal(environmentOperationalTone(lane), lane.lane_status.current.tone);
  assert.deepEqual(operationalAttentionReasons(lane), lane.lane_status.current.attention_reasons);
  assert.deepEqual(operationalWarningReasons(lane), lane.lane_status.current.warning_reasons);
  assert.equal(environmentReleaseHealthy(lane), lane.lane_status.current.release_healthy);
});

test("an open page follows service-computed expiry transitions without changing source evidence", () => {
  const deadline = Date.now();
  const lane = { lane_status: { current, transitions: [{ after: new Date(deadline).toISOString(), signal: expired }] } };
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
