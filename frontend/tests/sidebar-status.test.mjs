import assert from "node:assert/strict";
import { test } from "node:test";
import { sidebarStatus } from "../src/sidebar-status.ts";

/** @type {import("../src/generated/openapi.ts").ProductLaneSignal} */
const signal = { tone: "danger", sidebar_tone: "danger", description: "Alert; DNS; business check failed", reasons: ["business check failed"], attention_reasons: [], warning_reasons: [], release_healthy: false, public_observation_gap: null };
const lane = { lane_status: { evaluated_at: new Date().toISOString(), current: signal, transitions: [] } };

test("sidebar renders the service tone and complete description without inferring health", () => {
  assert.deepEqual(sidebarStatus(lane, "Testing"), { tone: signal.sidebar_tone, description: `Testing: ${signal.description}` });
});

test("absent lanes and responses without status cannot show healthy", () => {
  assert.equal(sidebarStatus(null, "Testing").tone, "missing");
  assert.equal(sidebarStatus({}, "Testing").tone, "missing");
});

test("stale inventory replaces cached failures and absent lanes with clocks", () => {
  for (const cached of [lane, null]) {
    const status = sidebarStatus(cached, "Testing", "Stale data; last refresh failed");
    assert.equal(status.tone, "stale");
    assert.doesNotMatch(status.description, /Alert|Warning|DNS/);
  }
});
