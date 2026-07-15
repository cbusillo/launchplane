import assert from "node:assert/strict";
import { test } from "node:test";

import { createBrowserOperationState, prepareBrowserOperation } from "../src/browser-operation.ts";
import {
  clearManagedSecretInputs,
  consumeManagedSecretValues,
  productConfigDraftLocked,
  productConfigManagedSecretIdentity,
  productConfigRuntimeDraftKey,
  productConfigSelectionKey,
} from "../src/product-config-operation.ts";

test("managed-secret values are consumed and cleared before request state is retained", async () => {
  const smtpIdentity = productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD");
  const analyticsIdentity = productConfigManagedSecretIdentity("analytics", "ANALYTICS_TOKEN");
  const inputs = new Map([
    [smtpIdentity, { value: "smtp-secret-value" }],
    [analyticsIdentity, { value: "analytics-secret-value" }],
  ]);

  const secrets = consumeManagedSecretValues(
    [
      {
        bindingKey: "SMTP_PASSWORD",
        integration: "runtime_environment",
        identity: smtpIdentity,
      },
      {
        bindingKey: "ANALYTICS_TOKEN",
        integration: "analytics",
        identity: analyticsIdentity,
      },
    ],
    inputs,
  );
  assert.deepEqual(
    [...inputs.values()].map((input) => input.value),
    ["", ""],
  );

  const operation = await prepareBrowserOperation("managed-secrets:apply", {
    mode: "apply",
    managed_secrets: secrets,
  });
  const retainedState = JSON.stringify(operation);
  assert.equal(retainedState.includes("smtp-secret-value"), false);
  assert.equal(retainedState.includes("analytics-secret-value"), false);
});

test("managed-secret validation errors clear every plaintext input", () => {
  const smtpIdentity = productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD");
  const analyticsIdentity = productConfigManagedSecretIdentity("analytics", "ANALYTICS_TOKEN");
  const inputs = new Map([
    [smtpIdentity, { value: "smtp-secret-value" }],
    [analyticsIdentity, { value: "" }],
  ]);

  assert.throws(
    () =>
      consumeManagedSecretValues(
        [
          {
            bindingKey: "SMTP_PASSWORD",
            integration: "runtime_environment",
            identity: smtpIdentity,
          },
          {
            bindingKey: "ANALYTICS_TOKEN",
            integration: "analytics",
            identity: analyticsIdentity,
          },
        ],
        inputs,
      ),
    /Enter a value for ANALYTICS_TOKEN/,
  );
  assert.deepEqual(
    [...inputs.values()].map((input) => input.value),
    ["", ""],
  );
});

test("route cleanup clears every managed-secret input", () => {
  const inputs = new Map([
    ["SMTP_PASSWORD", { value: "smtp-secret-value" }],
    ["ANALYTICS_TOKEN", { value: "analytics-secret-value" }],
  ]);

  clearManagedSecretInputs(inputs);

  assert.deepEqual(
    [...inputs.values()].map((input) => input.value),
    ["", ""],
  );
});

test("runtime and secret plan keys are stable across display order", () => {
  assert.equal(
    productConfigSelectionKey(["SMTP_PASSWORD", "ANALYTICS_TOKEN"]),
    productConfigSelectionKey(["ANALYTICS_TOKEN", "SMTP_PASSWORD"]),
  );
  assert.equal(
    productConfigRuntimeDraftKey(
      ["PUBLIC_ORIGIN", "SENDER_EMAIL"],
      { PUBLIC_ORIGIN: "https://example.invalid", SENDER_EMAIL: "ops@example.invalid" },
    ),
    productConfigRuntimeDraftKey(
      ["SENDER_EMAIL", "PUBLIC_ORIGIN"],
      { SENDER_EMAIL: "ops@example.invalid", PUBLIC_ORIGIN: "https://example.invalid" },
    ),
  );
  assert.notEqual(
    productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD"),
    productConfigManagedSecretIdentity("external_service", "SMTP_PASSWORD"),
  );
});

test("uncertain apply continuity locks every editable draft field", () => {
  const idle = createBrowserOperationState();
  const uncertain = {
    ...createBrowserOperationState(),
    phase: "uncertain",
    requiresIdempotencyContinuity: true,
  };

  assert.equal(productConfigDraftLocked(idle, uncertain), true);
  assert.equal(productConfigDraftLocked(idle, idle), false);
});
