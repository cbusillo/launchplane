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
    "/v1/drivers/generic-web/prod-promotion",
    "/v1/drivers/generic-web/prod-promotion-workflow",
    "/v1/product-config/apply",
    "/v1/work-graph/rank",
  ]));
});

test("generic-web promotion is classified as a planned dry run", () => {
  const presentation = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion",
      route_path: BROWSER_WRITE_ROUTES.genericWebPromotion,
    }),
  );

  assert.equal(presentation.kind, "dry-run");
  assert.equal(presentation.browserSupport, "planned");
  assert.equal(presentation.canExecute, false);
  assert.deepEqual(presentation.serverBlockers, []);
  assert.match(presentation.browserBlockers[0], /not installed yet/);
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
        route_path: BROWSER_WRITE_ROUTES.genericWebPromotion,
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
      route_path: BROWSER_WRITE_ROUTES.genericWebPromotionWorkflow,
    }),
  );

  assert.deepEqual(presentation.serverBlockers, [
    "Caller is not authorized for this action.",
    "Product profile does not define a prod lane.",
  ]);
  assert.match(presentation.browserBlockers[0], /not installed yet/);
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
  const planned = browserActionPresentation(
    "generic-web",
    action({
      action_id: "prod_promotion",
      route_path: BROWSER_WRITE_ROUTES.genericWebPromotion,
    }),
  );
  const unsupported = browserActionPresentation("generic-web", action());
  const implemented = {
    ...planned,
    browserBlockers: [],
    browserSupport: "implemented",
    canExecute: true,
  };

  const sorted = [unsupported, planned, implemented].sort(
    compareBrowserActionPresentations,
  );
  assert.deepEqual(
    sorted.map(({ browserSupport }) => browserSupport),
    ["implemented", "planned", "unsupported"],
  );
  assert.equal(
    Math.sign(compareBrowserActionPresentations(planned, unsupported)),
    -Math.sign(compareBrowserActionPresentations(unsupported, planned)),
  );
});
