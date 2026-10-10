import assert from "node:assert/strict";
import { test } from "node:test";

import {
  beginBrowserOperation, completeBrowserOperation, createBrowserOperationState,
  failBrowserOperation, markBrowserOperationDispatched, persistBrowserOperationState,
  prepareBrowserOperation, recoverBrowserOperationState, resetBrowserOperation,
  retryBrowserOperation,
} from "../src/browser-operation.ts";
import { LaunchplaneApiError } from "../src/api.ts";
import {
  clearManagedSecretInputs,
  consumeManagedSecretValues,
  productConfigDraftLocked,
  productConfigFailureCertainty,
  productConfigOperationFailure,
  productConfigManagedSecretIdentity,
  productConfigRuntimeChange,
  productConfigRuntimeChangeKey,
  productConfigSelectionKey,
  ProductConfigRecoveryStorageUnavailable,
  validateProductConfigApplyReplay,
} from "../src/product-config-operation.ts";

test("managed-secret values are consumed and cleared before request state is retained", async () => {
  const smtpIdentity = productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD");
  const analyticsIdentity = productConfigManagedSecretIdentity("analytics", "ANALYTICS_TOKEN");
  /** @type {Map<string, { value: string }>} */
  const inputs = new Map();
  inputs.set(smtpIdentity, { value: "smtp-secret-value" });
  inputs.set(analyticsIdentity, { value: "analytics-secret-value" });

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
  /** @type {Map<string, { value: string }>} */
  const inputs = new Map();
  inputs.set(smtpIdentity, { value: "smtp-secret-value" });
  inputs.set(analyticsIdentity, { value: "" });

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

test("selecting an Owner submission sends its version and discards any typed value", () => {
  const identity = productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD");
  /** @type {Map<string, {value: string}>} */
  const inputs = new Map();
  inputs.set(identity, { value: "discarded-typed-secret" });
  const result = consumeManagedSecretValues(
    [{ bindingKey: "SMTP_PASSWORD", integration: "runtime_environment", identity }],
    inputs,
    new Map([[identity, "owner-version-1"]]),
  );
  assert.deepEqual(result, [{ binding_key: "SMTP_PASSWORD", integration: "runtime_environment", owner_submission_version_id: "owner-version-1" }]);
  assert.equal(inputs.get(identity).value, "");
});

test("route cleanup clears every managed-secret input", () => {
  /** @type {Map<string, { value: string }>} */
  const inputs = new Map();
  inputs.set("SMTP_PASSWORD", { value: "smtp-secret-value" });
  inputs.set("ANALYTICS_TOKEN", { value: "analytics-secret-value" });

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
    productConfigRuntimeChangeKey(
      productConfigRuntimeChange(
        ["PUBLIC_ORIGIN", "SENDER_EMAIL"],
        { PUBLIC_ORIGIN: "https://example.invalid", SENDER_EMAIL: "ops@example.invalid" },
        [],
        "",
      ),
    ),
    productConfigRuntimeChangeKey(
      productConfigRuntimeChange(
        ["SENDER_EMAIL", "PUBLIC_ORIGIN"],
        { SENDER_EMAIL: "ops@example.invalid", PUBLIC_ORIGIN: "https://example.invalid" },
        [],
        "",
      ),
    ),
  );
  assert.notEqual(
    productConfigManagedSecretIdentity("runtime_environment", "SMTP_PASSWORD"),
    productConfigManagedSecretIdentity("external_service", "SMTP_PASSWORD"),
  );
});

test("a draft carries the site's own settings and provider keys to retire", () => {
  const change = productConfigRuntimeChange(
    ["PUBLIC_ORIGIN"],
    { PUBLIC_ORIGIN: "https://example.invalid" },
    [{ key: " SITE_MODE ", value: "full" }, { key: "", value: "ignored" }],
    "LEGACY_TUNING\nOLD_KEY, LEGACY_TUNING",
  );
  assert.deepEqual(change, {
    runtime_settings: { PUBLIC_ORIGIN: "https://example.invalid", SITE_MODE: "full" },
    retired_provider_keys: ["LEGACY_TUNING", "OLD_KEY"],
  });
  assert.throws(
    () =>
      productConfigRuntimeChange(
        ["PUBLIC_ORIGIN"],
        { PUBLIC_ORIGIN: "a" },
        [{ key: "PUBLIC_ORIGIN", value: "b" }],
        "",
      ),
    /PUBLIC_ORIGIN appears more than once/,
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
  assert.equal(productConfigDraftLocked(idle, uncertain, true), false);
  assert.equal(productConfigDraftLocked(idle, { ...uncertain, phase: "submitting" }, true), true);
  assert.equal(productConfigDraftLocked(idle, idle), false);
});

test("a JSON gateway failure after Apply preserves exact-operation recovery", async () => {
  for (const kind of ["runtime-settings", "managed-secrets"]) {
    const scope = `example:testing:${kind}:apply`;
    const request = kind === "runtime-settings"
      ? { runtime_settings: { SITE_TITLE: "Example" } }
      : { managed_secrets: [{ binding_key: "SMTP_PASSWORD", value: "inert-test-secret" }] };
    const prepared = await prepareBrowserOperation(scope, request);
    const dispatched = markBrowserOperationDispatched(beginBrowserOperation(prepared));
    // The service committed this identity; only the response was replaced by a gateway error.
    const committedKey = dispatched.identity.idempotencyKey;
    const error = new LaunchplaneApiError("Gateway unavailable", 502, "gateway-trace", "gateway_error");
    const failed = failBrowserOperation(dispatched, productConfigOperationFailure(error),
      productConfigFailureCertainty(error, true));
    assert.equal(productConfigDraftLocked(createBrowserOperationState(), failed), true);
    assert.throws(() => resetBrowserOperation(failed), /uncertain/);
    const values = new Map();
    const storage = { getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
    persistBrowserOperationState(scope, failed, storage);
    assert.equal([...values.values()].some((value) => value.includes("inert-test-secret")), false);
    const recovered = recoverBrowserOperationState(scope, storage);
    await assert.rejects(prepareBrowserOperation(scope, { replacement: true }, recovered), /uncertain/);
    const retry = retryBrowserOperation(recovered);
    assert.equal((await prepareBrowserOperation(scope, request, retry)).identity.idempotencyKey, committedKey);
    const replay = completeBrowserOperation(markBrowserOperationDispatched(beginBrowserOperation(retry)),
      { trace_id: "retry-trace", original_trace_id: "commit-trace", replayed: true });
    persistBrowserOperationState(scope, replay, storage);
    assert.equal(replay.requiresIdempotencyContinuity, false);
    assert.equal(replay.receipt.replayed, true);
    assert.equal(recoverBrowserOperationState(scope, storage).phase, "idle");
  }
});

test("configuration failure certainty distinguishes dispatch from proven refusals", () => {
  for (const status of [500, 502, 503]) {
    const error = new LaunchplaneApiError("Unavailable", status, "trace", "unavailable");
    assert.equal(productConfigFailureCertainty(error, true), "uncertain");
    assert.equal(productConfigFailureCertainty(error, false), "definitive");
  }
  for (const status of [400, 403, 409, 422]) {
    assert.equal(productConfigFailureCertainty(
      new LaunchplaneApiError("Refused", status, "trace", "refused"), true), "definitive");
  }
  for (const code of ["secret_configuration_required", "secret_storage_unavailable"]) {
    assert.equal(productConfigFailureCertainty(
      new LaunchplaneApiError("Readiness refused", 503, "trace", code), true), "definitive");
  }
  assert.equal(productConfigFailureCertainty(new ProductConfigRecoveryStorageUnavailable("Not sent"), true), "definitive");
});

test("a rejected re-entry leaves original uncertainty evidence intact", async () => {
  const scope = "example:testing:managed-secrets:apply";
  const request = { managed_secrets: [{ binding_key: "SMTP_PASSWORD", value: "inert-secret" }] };
  const dispatched = markBrowserOperationDispatched(beginBrowserOperation(await prepareBrowserOperation(scope, request)));
  const original = failBrowserOperation(dispatched, { code: "gateway_error", message: "Unavailable", statusCode: 502, traceId: "original-trace" }, "uncertain");
  await assert.rejects(validateProductConfigApplyReplay(scope, { replacement: true }, original), /uncertain/);
  assert.equal(original.failure.traceId, "original-trace");
  await validateProductConfigApplyReplay(scope, request, original);
  const retry = markBrowserOperationDispatched(beginBrowserOperation(retryBrowserOperation(original)));
  const prewrite = new LaunchplaneApiError("Storage unavailable", 503, "retry", "secret_storage_unavailable");
  assert.equal(failBrowserOperation(retry, productConfigOperationFailure(prewrite), productConfigFailureCertainty(prewrite, true)).requiresIdempotencyContinuity, true);
});
