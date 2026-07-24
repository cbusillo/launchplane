import assert from "node:assert/strict";
import test from "node:test";

import {
  buildOperationalReadinessRequest,
  inspectableReadinessActions,
} from "../src/readiness-model.ts";

function action({ actionId, authzAction }) {
  return {
    action_id: actionId,
    alternate_authz_actions: [],
    authz_action: authzAction,
    description: "",
    disabled_reasons: [],
    enabled: true,
    label: actionId,
    method: "POST",
    route_path: `/v1/${actionId}`,
    safety: "read",
    scope: "instance",
    trust_state: "recorded",
  };
}

function detail({ manifestArtifact = "", runtimeArtifact = "" } = {}) {
  return {
    product: "demo-product",
    context: "stable-context",
    environment: "testing",
    target: {
      artifact_manifest: manifestArtifact
        ? { artifact_id: manifestArtifact }
        : null,
      expected_runtime_identity: runtimeArtifact
        ? { artifact_id: runtimeArtifact }
        : null,
    },
  };
}

test("inspectable readiness actions require one exact primary authz action", () => {
  const unique = action({ actionId: "unique", authzAction: "demo.unique" });
  const duplicateA = action({ actionId: "duplicate-a", authzAction: "demo.duplicate" });
  const duplicateB = action({ actionId: "duplicate-b", authzAction: "demo.duplicate" });
  const missing = action({ actionId: "missing", authzAction: " " });

  assert.deepEqual(
    inspectableReadinessActions([unique, duplicateA, duplicateB, missing]),
    [unique],
  );
});

test("alternate authz collisions are not presented as exact primary actions", () => {
  const primary = action({ actionId: "primary", authzAction: "demo.primary" });
  const alternate = {
    ...action({ actionId: "alternate", authzAction: "demo.alternate" }),
    alternate_authz_actions: ["demo.primary"],
  };

  assert.deepEqual(inspectableReadinessActions([primary, alternate]), [alternate]);
});

test("readiness request uses Launchplane-owned lane and artifact records", () => {
  const request = buildOperationalReadinessRequest(
    detail({ manifestArtifact: "artifact-candidate", runtimeArtifact: "artifact-current" }),
    action({ actionId: "deploy", authzAction: "demo.deploy" }),
  );

  assert.deepEqual(request, {
    path: {
      product: "demo-product",
      context: "stable-context",
      instance: "testing",
    },
    query: {
      action: "demo.deploy",
      artifact_id: "artifact-candidate",
      expected_current_artifact_id: "artifact-current",
    },
  });
});

test("readiness request omits artifact selector when the manifest is missing", () => {
  assert.deepEqual(
    buildOperationalReadinessRequest(
      detail({ runtimeArtifact: "artifact-current" }),
      action({ actionId: "inspect", authzAction: "demo.inspect" }),
    ).query,
    {
      action: "demo.inspect",
      expected_current_artifact_id: "artifact-current",
    },
  );
  assert.deepEqual(
    buildOperationalReadinessRequest(
      detail(),
      action({ actionId: "inspect", authzAction: "demo.inspect" }),
    ).query,
    { action: "demo.inspect" },
  );
});

test("candidate artifact never claims current deployment authority", () => {
  const request = buildOperationalReadinessRequest(
    detail({ manifestArtifact: "artifact-candidate" }),
    action({ actionId: "replace", authzAction: "demo.replace" }),
  );

  assert.deepEqual(request.query, {
    action: "demo.replace",
    artifact_id: "artifact-candidate",
  });
});
