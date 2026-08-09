import assert from "node:assert/strict";
import test from "node:test";

import {
  ISSUE_RECONCILIATION_BROWSER_BOUNDARY,
  MERGE_TRAIN_BROWSER_BOUNDARY,
  filterWorkGraphItems,
  mergeTrainControllerTone,
  mergeTrainStatusScopeKey,
  mergeTrainTargetSelection,
  mergeTrainTargetOptions,
  ownerAcceptanceBindingEligibility,
  scalarEvidence,
  workGraphRankScopeKey,
} from "../src/engineering-model.ts";
import {
  cancelledEngineeringResource,
  engineeringResourceForScope,
  emptyEngineeringResource,
  engineeringFailure,
} from "../src/engineering-resource.ts";

test("engineering write boundaries stay explicit and unsupported", () => {
  assert.match(ISSUE_RECONCILIATION_BROWSER_BOUNDARY, /GitHub Actions OIDC/);
  assert.match(ISSUE_RECONCILIATION_BROWSER_BOUNDARY, /absent from the generated browser write contract/);
  assert.match(MERGE_TRAIN_BROWSER_BOUNDARY, /never executes a worker route dynamically/);
});

test("Owner review eligibility fails closed on route or binding mismatch", () => {
  const eligible = {
    schema_version: 1,
    binding_sha256: "a".repeat(64),
    product: "example-product",
    system: "website",
    action: "pull_request.owner_acceptance",
    environment: "pull_request",
    can_submit_event: true,
    reason_code: "current_product_owner",
  };
  assert.equal(
    ownerAcceptanceBindingEligibility(
      { event_write_authorized: true, bindings: [eligible] },
      eligible.binding_sha256,
    ),
    eligible,
  );
  assert.equal(
    ownerAcceptanceBindingEligibility(
      { event_write_authorized: true, bindings: [eligible] },
      "b".repeat(64),
    ),
    undefined,
  );
  assert.equal(
    ownerAcceptanceBindingEligibility(
      { event_write_authorized: false, bindings: [eligible] },
      eligible.binding_sha256,
    ),
    undefined,
  );
});

test("merge train targets come only from policy targets", () => {
  const targets = mergeTrainTargetOptions([
    {
      repository: "example/zeta",
      base_branch: "main",
      policy_key: "zeta:main",
      scheduler: { enabled: true, mutate: false, runner_mode: "controller" },
      service_authz: {
        action: "merge_train.run_once",
        product: "launchplane",
        context: "launchplane",
      },
    },
    {
      repository: "example/alpha",
      base_branch: "release",
      policy_key: "alpha:release",
      scheduler: { enabled: false, mutate: false, runner_mode: "level1" },
      service_authz: {
        action: "merge_train.run_once",
        product: "launchplane",
        context: "launchplane",
      },
    },
  ]);

  assert.deepEqual(
    targets.map((target) => target.key),
    ["example/alpha:release", "example/zeta:main"],
  );
  assert.equal(targets[0].label, "example/alpha · release");
  assert.equal(targets[0].target.scheduler.runner_mode, "level1");
});

test("partial merge train target queries fail closed", () => {
  assert.deepEqual(mergeTrainTargetSelection(""), { invalid: false, key: "" });
  assert.deepEqual(mergeTrainTargetSelection("?repository=example%2Fcontrol-plane"), {
    invalid: true,
    key: "",
  });
  assert.deepEqual(mergeTrainTargetSelection("?base_branch=main"), {
    invalid: true,
    key: "",
  });
  assert.deepEqual(mergeTrainTargetSelection("?repository=&base_branch="), {
    invalid: true,
    key: "",
  });
  assert.deepEqual(
    mergeTrainTargetSelection(
      "?repository=example%2Fcontrol-plane&repository=&base_branch=main",
    ),
    { invalid: true, key: "" },
  );
  assert.deepEqual(
    mergeTrainTargetSelection(
      "?repository=example%2Fcontrol-plane&base_branch=main&base_branch=release",
    ),
    { invalid: true, key: "" },
  );
  assert.deepEqual(
    mergeTrainTargetSelection(
      "?repository=example%2Fcontrol-plane&base_branch=release%2Fstable",
    ),
    {
      invalid: false,
      key: "example/control-plane:release/stable",
    },
  );
});

test("work graph filters preserve generated recommendation evidence", () => {
  const items = [
    { state: "ready", recommendation: "quick_win", title: "Ready" },
    { state: "blocked", recommendation: "attention_needed", title: "Blocked" },
  ];
  assert.deepEqual(
    filterWorkGraphItems(items, "ready", "quick_win").map((item) => item.title),
    ["Ready"],
  );
  assert.deepEqual(filterWorkGraphItems(items, "all", "all"), items);
});

test("controller reconciliation and stale policy evidence fail closed", () => {
  const base = {
    controller_records: [],
    controller_state: null,
    controller_diagnostics: null,
  };
  assert.equal(
    mergeTrainControllerTone({
      ...base,
      controller_state: { status: "reconcile_required" },
    }),
    "blocked",
  );
  assert.equal(
    mergeTrainControllerTone({
      ...base,
      controller_records: [{ policy_status: "stale" }],
    }),
    "blocked",
  );
  assert.equal(
    mergeTrainControllerTone({
      ...base,
      controller_state: { status: "running" },
    }),
    "pending",
  );
  assert.equal(mergeTrainControllerTone(base), "unknown");
  assert.equal(
    mergeTrainControllerTone(
      { ...base, current_policy_sha256: "stale-digest" },
      "active-digest",
    ),
    "blocked",
  );
});

test("engineering errors distinguish denied from unavailable", () => {
  assert.deepEqual(
    engineeringFailure({
      message: "Forbidden",
      statusCode: 403,
      traceId: "trace-denied",
    }),
    {
      message: "Forbidden",
      statusCode: 403,
      traceId: "trace-denied",
      denied: true,
    },
  );
  assert.equal(engineeringFailure(new Error("offline")).denied, false);
});

test("cancellation is terminal without data and stale with cached data", () => {
  const initial = cancelledEngineeringResource({
    ...emptyEngineeringResource(),
    phase: "loading",
    refreshing: true,
  });
  assert.equal(initial.phase, "cancelled");
  assert.equal(initial.refreshing, false);

  const cached = cancelledEngineeringResource({
    ...emptyEngineeringResource(),
    phase: "ready",
    data: { value: "accepted" },
    refreshing: true,
    lastSuccessfulAt: "2026-07-15T00:00:00Z",
  });
  assert.equal(cached.phase, "ready");
  assert.equal(cached.cancelled, true);
  assert.equal(cached.stale, true);
  assert.deepEqual(cached.data, { value: "accepted" });
});

test("dependent resource data is hidden before a new scope settles", () => {
  const ready = {
    ...emptyEngineeringResource(),
    phase: "ready",
    data: { target: "first" },
  };
  assert.equal(engineeringResourceForScope(ready, "first", "first", true), ready);
  const changed = engineeringResourceForScope(ready, "first", "second", true);
  assert.equal(changed.phase, "idle");
  assert.equal(changed.data, null);
  const disabled = engineeringResourceForScope(ready, "first", "first", false);
  assert.equal(disabled.phase, "idle");
  assert.equal(disabled.data, null);
});

test("dependent route scope keys change with every accepted parent scope", () => {
  const rankScope = workGraphRankScopeKey(
    "2026-07-15T04:00:00Z",
    "trace-one",
    "2026-07-15T03:59:00Z",
    "products",
  );
  assert.notEqual(
    rankScope,
    workGraphRankScopeKey(
      "2026-07-15T04:01:00Z",
      "trace-one",
      "2026-07-15T03:59:00Z",
      "products",
    ),
  );

  const statusScope = mergeTrainStatusScopeKey(
    "example/control-plane:main",
    "2026-07-15T04:00:00Z",
    "policy-one",
    "products",
  );
  assert.notEqual(
    statusScope,
    mergeTrainStatusScopeKey(
      "example/runtime-site:main",
      "2026-07-15T04:00:00Z",
      "policy-one",
      "products",
    ),
  );
  assert.notEqual(
    statusScope,
    mergeTrainStatusScopeKey(
      "example/control-plane:main",
      "2026-07-15T04:01:00Z",
      "policy-two",
      "products",
    ),
  );
});

test("snapshot source evidence exposes scalar facts only", () => {
  assert.deepEqual(
    scalarEvidence({ count: 2, enabled: true, nested: { hidden: true }, name: "project" }),
    [
      { label: "count", value: "2" },
      { label: "enabled", value: "yes" },
      { label: "name", value: "project" },
    ],
  );
});
