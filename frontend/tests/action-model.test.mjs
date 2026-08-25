import assert from "node:assert/strict";
import test from "node:test";

import {
  browserActionPresentation,
  compareBrowserActionPresentations,
} from "../src/action-model.ts";
import { BROWSER_WRITE_ROUTES } from "../src/browser-write-contract.ts";

function action(overrides = {}) {
  return {
    action_id: "stable_deploy",
    alternate_authz_actions: [],
    authz_action: "deployment.write",
    description: "Deploy a stable lane.",
    disabled_reasons: [],
    enabled: true,
    label: "Deploy lane",
    method: "POST",
    route_path: "/v1/drivers/generic-web/deploy",
    safety: "mutation",
    scope: "instance",
    trust_state: "recorded",
    ...overrides,
  };
}

test("browser write routes are the generated UI write allowlist", () => {
  assert.deepEqual(new Set(Object.values(BROWSER_WRITE_ROUTES)), new Set([
    "/v1/authorization-recovery/keys/enroll",
    "/v1/authorization-recovery/keys/{key_id}/verify",
    "/v1/authorization-recovery/keys/{key_id}/revoke",
    "/v1/products/{product}/environments/{environment}/config/apply",
    "/v1/products/{product}/environments/{environment}/promotion/dry-run",
    "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch",
    "/v1/work-graph/rank",
    "/v1/owner-acceptance/events",
    "/v1/privileged-operations/plans/{operation_id}/approve",
    "/v1/privileged-operations/plans/{operation_id}/revoke",
  ]));
});

test("generic-web promotion descriptors stay diagnostics for the product-owned flow", () => {
  const presentation = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion",
      route_path: "/v1/drivers/generic-web/prod-promotion",
    }),
  );

  assert.equal(presentation.kind, "dry-run");
  assert.equal(presentation.browserSupport, "diagnostic");
  assert.equal(presentation.canExecute, false);
  assert.deepEqual(presentation.serverBlockers, []);
  assert.deepEqual(presentation.browserBlockers, []);
});

test("advertised route drift fails closed instead of executing route_path", () => {
  const presentation = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion",
      route_path: "/v1/drivers/generic-web/unexpected",
    }),
  );

  assert.equal(presentation.kind, "unsupported");
  assert.equal(presentation.browserSupport, "unsupported");
  assert.equal(presentation.canExecute, false);
  assert.match(presentation.browserBlockers[0], /does not match/);
});

test("advertised method, safety, or scope drift fails closed", () => {
  for (const drift of [
    { method: "GET" },
    { safety: "destructive" },
    { scope: "context" },
  ]) {
    const presentation = browserActionPresentation(
      "generic-web",
      action({
        action_id: "prod_promotion",
        route_path: "/v1/drivers/generic-web/prod-promotion",
        ...drift,
      }),
    );

    assert.equal(presentation.kind, "unsupported");
    assert.equal(presentation.browserSupport, "unsupported");
    assert.equal(presentation.canExecute, false);
    assert.match(presentation.browserBlockers[0], /safety, or scope/);
  }
});

test("server blockers remain exact and separate from browser blockers", () => {
  const presentation = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion_workflow",
      disabled_reasons: [
        "Caller is not authorized for this action.",
        "Product profile does not define a prod lane.",
      ],
      enabled: false,
      route_path: "/v1/drivers/generic-web/prod-promotion-workflow",
    }),
  );

  assert.deepEqual(presentation.serverBlockers, [
    "Caller is not authorized for this action.",
    "Product profile does not define a prod lane.",
  ]);
  assert.deepEqual(presentation.browserBlockers, []);
  assert.equal(presentation.canExecute, false);
});

test("unsupported actions still disclose destructive and inspect behavior", () => {
  const destructive = browserActionPresentation(
    "generic-web",
    action({ action_id: "prod_rollback", safety: "destructive" }),
  );
  const inspect = browserActionPresentation(
    "generic-web",
    action({ action_id: "preview_readiness", safety: "read" }),
  );

  assert.equal(destructive.kind, "destructive");
  assert.equal(inspect.kind, "inspect");
  assert.equal(destructive.canExecute, false);
  assert.equal(inspect.canExecute, false);
});

test("action ordering is deterministic across every support state", () => {
  const diagnostic = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion",
      route_path: "/v1/drivers/generic-web/prod-promotion",
    }),
  );
  const unsupported = browserActionPresentation("generic-web", action());
  const implemented = {
    ...diagnostic,
    browserSupport: "implemented",
    canExecute: true,
  };
  const planned = {
    ...diagnostic,
    browserBlockers: ["Typed flow pending."],
    browserSupport: "planned",
    canExecute: false,
  };

  const sorted = [unsupported, planned, diagnostic, implemented].sort(
    compareBrowserActionPresentations,
  );
  assert.deepEqual(
    sorted.map(({ browserSupport }) => browserSupport),
    ["implemented", "diagnostic", "planned", "unsupported"],
  );
  assert.equal(
    Math.sign(compareBrowserActionPresentations(planned, unsupported)),
    -Math.sign(compareBrowserActionPresentations(unsupported, planned)),
  );
});
