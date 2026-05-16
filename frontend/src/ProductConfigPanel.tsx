import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Play,
  Plus,
  Save,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { LaunchplaneApiError, applyProductConfig } from "./api";
import { KeyValue, PanelHead } from "./panel-ui";
import { StatusPill } from "./status-ui";
import type {
  ProductConfigApplyPayload,
  ProductConfigApplyRequest,
  ProductConfigRuntimeScope,
  ProductConfigSecretScope,
  ProductEnvironmentSummary,
} from "./types";

type RuntimeConfigRow = {
  id: string;
  key: string;
  value: string;
};

type SecretConfigRow = {
  id: string;
  name: string;
  bindingKey: string;
  value: string;
  description: string;
};

const PRODUCT_CONFIG_REQUEST_TIMEOUT_MS = 30_000;

export function ProductConfigPanel({
  productDefault,
  contextDefault,
  instanceDefault,
  environments,
  disabled,
  applyConfig = applyProductConfig,
}: {
  productDefault: string;
  contextDefault: string;
  instanceDefault: string;
  environments: ProductEnvironmentSummary[];
  disabled: boolean;
  applyConfig?: (
    payload: ProductConfigApplyRequest,
    signal?: AbortSignal,
  ) => Promise<ProductConfigApplyPayload>;
}) {
  const [targetEnvironment, setTargetEnvironment] = useState(instanceDefault);
  const [sourceLabel, setSourceLabel] = useState("product-config-ui");
  const [runtimeRows, setRuntimeRows] = useState<RuntimeConfigRow[]>(() => [
    newRuntimeConfigRow(),
  ]);
  const [secretRows, setSecretRows] = useState<SecretConfigRow[]>(() => [
    newSecretConfigRow(),
  ]);
  const [result, setResult] = useState<ProductConfigApplyPayload | null>(null);
  const [pendingApplyPayload, setPendingApplyPayload] =
    useState<ProductConfigApplyRequest | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [submitting, setSubmitting] = useState<
    ProductConfigApplyRequest["mode"] | null
  >(null);
  const [panelError, setPanelError] = useState("");
  const [traceId, setTraceId] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const requestSequence = useRef(0);

  const environmentOptions = useMemo(
    () =>
      environments.filter(
        (environment) =>
          environment.environment.trim() && environment.context.trim(),
      ),
    [environments],
  );

  const selectedEnvironment =
    environmentOptions.find(
      (environment) => environment.environment === targetEnvironment,
    ) ??
    environmentOptions.find(
      (environment) => environment.environment === instanceDefault,
    ) ??
    environmentOptions[0] ??
    null;
  const contextName = (selectedEnvironment?.context ?? contextDefault).trim();
  const instanceName = (
    selectedEnvironment?.environment ??
    (targetEnvironment || instanceDefault)
  ).trim();
  const product = productDefault.trim();

  useEffect(() => {
    const nextEnvironment =
      environmentOptions.find(
        (environment) => environment.environment === instanceDefault,
      )?.environment ??
      environmentOptions[0]?.environment ??
      instanceDefault;
    setTargetEnvironment(nextEnvironment);
    clearReviewState();
  }, [productDefault, contextDefault, instanceDefault, environmentOptions]);

  const runtimeScope = runtimeScopeForTarget(contextName, instanceName);
  const secretScope = secretScopeForTarget(contextName, instanceName);
  const runtimeInputCount = runtimeRows.filter((row) => row.key.trim()).length;
  const completeSecretRows = secretRows.filter(
    (row) => row.name.trim() && row.bindingKey.trim() && row.value.trim(),
  );
  const partialSecretRows = secretRows.filter((row) => {
    const filledFields = [row.name, row.bindingKey, row.value].filter((value) =>
      value.trim(),
    ).length;
    return filledFields > 0 && filledFields < 3;
  });
  const actionStatus = productConfigActionStatus({
    submitting,
    panelError,
    successMessage,
  });
  const showPartialSecretWarning =
    partialSecretRows.length > 0 && !result && !panelError && !submitting;
  const hasConfigInput = runtimeInputCount > 0 || completeSecretRows.length > 0;
  const canDryRun = Boolean(
    product.trim() &&
    hasConfigInput &&
    partialSecretRows.length === 0 &&
    !disabled &&
    !submitting,
  );
  const canApply = Boolean(
    pendingApplyPayload && reviewed && !disabled && !submitting,
  );

  function clearReviewState() {
    requestSequence.current += 1;
    setResult(null);
    setPendingApplyPayload(null);
    setReviewed(false);
    setSubmitting(null);
    setPanelError("");
    setTraceId("");
    setSuccessMessage("");
  }

  function clearRenderedSecretValues() {
    setSecretRows((rows) => rows.map((row) => ({ ...row, value: "" })));
  }

  function updateRuntimeRow(rowId: string, patch: Partial<RuntimeConfigRow>) {
    clearReviewState();
    setRuntimeRows((rows) =>
      rows.map((row) => (row.id === rowId ? { ...row, ...patch } : row)),
    );
  }

  function updateSecretRow(rowId: string, patch: Partial<SecretConfigRow>) {
    clearReviewState();
    setSecretRows((rows) =>
      rows.map((row) => (row.id === rowId ? { ...row, ...patch } : row)),
    );
  }

  function addRuntimeRow() {
    clearReviewState();
    setRuntimeRows((rows) => [...rows, newRuntimeConfigRow()]);
  }

  function addSecretRow() {
    clearReviewState();
    setSecretRows((rows) => [...rows, newSecretConfigRow()]);
  }

  function removeRuntimeRow(rowId: string) {
    clearReviewState();
    setRuntimeRows((rows) => rows.filter((row) => row.id !== rowId));
  }

  function removeSecretRow(rowId: string) {
    clearReviewState();
    setSecretRows((rows) => rows.filter((row) => row.id !== rowId));
  }

  function buildPayload(
    mode: ProductConfigApplyRequest["mode"],
  ): ProductConfigApplyRequest {
    const runtimeEnv = runtimeRows.reduce<Record<string, string>>(
      (env, row) => {
        const key = row.key.trim();
        if (key) {
          env[key] = row.value;
        }
        return env;
      },
      {},
    );
    const secrets = completeSecretRows.map((row) => ({
      scope: secretScope,
      context: contextName.trim(),
      instance: instanceName.trim(),
      name: row.name.trim(),
      binding_key: row.bindingKey.trim(),
      value: row.value,
      description: row.description.trim(),
    }));
    const payload: ProductConfigApplyRequest = {
      schema_version: 1,
      mode,
      product: product.trim(),
      context: contextName.trim(),
      instance: instanceName.trim(),
      source_label: sourceLabel.trim() || "product-config-ui",
    };
    if (Object.keys(runtimeEnv).length) {
      payload.runtime_env = {
        scope: runtimeScope,
        context: contextName.trim(),
        instance: instanceName.trim(),
        env: runtimeEnv,
      };
    }
    if (secrets.length) {
      payload.secrets = secrets;
    }
    return payload;
  }

  function runDryRun() {
    const payload = buildPayload("dry-run");
    requestSequence.current += 1;
    const requestId = requestSequence.current;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      PRODUCT_CONFIG_REQUEST_TIMEOUT_MS,
    );
    setSubmitting("dry-run");
    setPanelError("");
    setTraceId("");
    setSuccessMessage("Running dry run...");
    applyConfig(payload, controller.signal)
      .then((payloadResult) => {
        if (requestSequence.current !== requestId) {
          return;
        }
        setResult(payloadResult);
        setPendingApplyPayload({ ...payload, mode: "apply" });
        clearRenderedSecretValues();
        setReviewed(false);
        setSuccessMessage(
          "Dry run completed. Review the plan before applying.",
        );
      })
      .catch((apiError: unknown) => {
        if (requestSequence.current !== requestId) {
          return;
        }
        setSuccessMessage("");
        if (apiError instanceof LaunchplaneApiError) {
          setPanelError(apiError.message);
          setTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setPanelError(productConfigRequestErrorMessage(apiError, "dry run"));
        } else {
          setPanelError("Product config request failed.");
        }
      })
      .finally(() => {
        window.clearTimeout(timeoutId);
        if (requestSequence.current === requestId) {
          setSubmitting(null);
        }
      });
  }

  function runApply() {
    if (!pendingApplyPayload) {
      return;
    }
    const payload = pendingApplyPayload;
    requestSequence.current += 1;
    const requestId = requestSequence.current;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      PRODUCT_CONFIG_REQUEST_TIMEOUT_MS,
    );
    setSubmitting("apply");
    setPanelError("");
    setTraceId("");
    setSuccessMessage("Applying product config...");
    applyConfig(payload, controller.signal)
      .then((payloadResult) => {
        if (requestSequence.current !== requestId) {
          return;
        }
        clearRenderedSecretValues();
        setResult(payloadResult);
        setPendingApplyPayload(null);
        setReviewed(false);
        setSuccessMessage(applySuccessMessage(payloadResult));
      })
      .catch((apiError: unknown) => {
        if (requestSequence.current !== requestId) {
          return;
        }
        setPendingApplyPayload(payload);
        setReviewed(true);
        setSuccessMessage("");
        if (apiError instanceof LaunchplaneApiError) {
          setPanelError(apiError.message);
          setTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setPanelError(productConfigRequestErrorMessage(apiError, "apply"));
        } else {
          setPanelError("Product config apply failed.");
        }
      })
      .finally(() => {
        window.clearTimeout(timeoutId);
        if (requestSequence.current === requestId) {
          setSubmitting(null);
        }
      });
  }

  return (
    <section className="panel product-config-panel">
      <PanelHead
        eyebrow="write config"
        title="Apply runtime config change"
        right={
          <StatusPill
            status={
              result?.mode === "apply"
                ? "pass"
                : pendingApplyPayload
                  ? "pending"
                  : "unknown"
            }
          />
        }
      />
      <div className="config-target-grid">
        <div className="config-label config-readonly-label">
          <span>Product ID</span>
          <strong>{product || "No product selected"}</strong>
        </div>
        <label className="config-label">
          <span>Environment</span>
          <select
            onChange={(event) => {
              clearReviewState();
              setTargetEnvironment(event.target.value);
            }}
            value={targetEnvironment}
          >
            {environmentOptions.length ? (
              environmentOptions.map((environment) => (
                <option
                  key={`${environment.context}:${environment.environment}`}
                  value={environment.environment}
                >
                  {environment.environment} ({environment.context})
                </option>
              ))
            ) : (
              <option value={targetEnvironment || instanceDefault}>
                {instanceName || "No environment target"}
              </option>
            )}
          </select>
        </label>
        <div className="config-label config-readonly-label">
          <span>Routing target</span>
          <div className="config-derived-routing">
            <code>{contextName || "missing-context"}</code>
            <small>{instanceName || "missing-instance"}</small>
          </div>
        </div>
        <label className="config-label">
          <span>Source</span>
          <input
            value={sourceLabel}
            onChange={(event) => {
              clearReviewState();
              setSourceLabel(event.target.value);
            }}
            spellCheck={false}
          />
        </label>
      </div>
      <div className="config-edit-grid">
        <div className="config-editor">
          <div className="config-editor-head">
            <div>
              <p className="eyebrow">runtime env</p>
              <strong>{runtimeScope}</strong>
            </div>
            <button
              className="icon-button"
              type="button"
              title="Add runtime key"
              aria-label="Add runtime key"
              onClick={addRuntimeRow}
            >
              <Plus size={15} />
            </button>
          </div>
          <div className="config-row-list">
            {runtimeRows.map((row) => (
              <div className="config-row config-row-runtime" key={row.id}>
                <input
                  aria-label="Runtime key"
                  placeholder="KEY"
                  value={row.key}
                  onChange={(event) =>
                    updateRuntimeRow(row.id, { key: event.target.value })
                  }
                  spellCheck={false}
                />
                <input
                  aria-label="Runtime value"
                  placeholder="value"
                  value={row.value}
                  onChange={(event) =>
                    updateRuntimeRow(row.id, { value: event.target.value })
                  }
                  spellCheck={false}
                />
                <button
                  className="icon-button"
                  type="button"
                  title="Remove runtime key"
                  aria-label="Remove runtime key"
                  disabled={runtimeRows.length === 1}
                  onClick={() => removeRuntimeRow(row.id)}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            ))}
          </div>
        </div>
        <div className="config-editor">
          <div className="config-editor-head">
            <div>
              <p className="eyebrow">managed secrets</p>
              <strong>{secretScope}</strong>
            </div>
            <button
              className="icon-button"
              type="button"
              title="Add secret"
              aria-label="Add secret"
              onClick={addSecretRow}
            >
              <Plus size={15} />
            </button>
          </div>
          <div className="config-row-list">
            {secretRows.map((row) => (
              <div className="config-row config-row-secret" key={row.id}>
                <input
                  aria-label="Secret name"
                  placeholder="SECRET_NAME"
                  value={row.name}
                  onChange={(event) =>
                    updateSecretRow(row.id, {
                      name: event.target.value,
                      bindingKey: row.bindingKey || event.target.value,
                    })
                  }
                  spellCheck={false}
                />
                <input
                  aria-label="Secret binding key"
                  placeholder="BINDING_KEY"
                  value={row.bindingKey}
                  onChange={(event) =>
                    updateSecretRow(row.id, { bindingKey: event.target.value })
                  }
                  spellCheck={false}
                />
                <input
                  aria-label="Secret value"
                  placeholder="value"
                  type="password"
                  value={row.value}
                  onChange={(event) =>
                    updateSecretRow(row.id, { value: event.target.value })
                  }
                  autoComplete="off"
                  spellCheck={false}
                />
                <input
                  aria-label="Secret description"
                  placeholder="description"
                  value={row.description}
                  onChange={(event) =>
                    updateSecretRow(row.id, { description: event.target.value })
                  }
                  spellCheck={false}
                />
                <button
                  className="icon-button"
                  type="button"
                  title="Remove secret"
                  aria-label="Remove secret"
                  disabled={secretRows.length === 1}
                  onClick={() => removeSecretRow(row.id)}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
      {showPartialSecretWarning ? (
        <div className="config-inline-alert" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>Complete or remove partial secret rows.</span>
        </div>
      ) : null}
      {panelError ? (
        <div className="config-inline-alert" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{panelError}</span>
          {traceId ? <code>{traceId}</code> : null}
        </div>
      ) : null}
      {successMessage ? (
        <div
          className="config-inline-alert config-inline-alert-success"
          role="status"
        >
          <CheckCircle2 size={15} aria-hidden="true" />
          <span>{successMessage}</span>
        </div>
      ) : null}
      {result ? <ProductConfigResult result={result} /> : null}
      <div className="config-actions">
        <button
          className="button"
          type="button"
          disabled={!canDryRun}
          onClick={runDryRun}
        >
          {submitting === "dry-run" ? (
            <Loader2 className="spin" size={15} />
          ) : (
            <Play size={15} />
          )}
          <span>Dry run</span>
        </button>
        <label className="config-review-check">
          <input
            type="checkbox"
            checked={reviewed}
            disabled={!pendingApplyPayload || Boolean(submitting)}
            onChange={(event) => setReviewed(event.target.checked)}
          />
          <span>Reviewed dry run</span>
        </label>
        <button
          className="button button-primary"
          type="button"
          disabled={!canApply}
          onClick={runApply}
        >
          {submitting === "apply" ? (
            <Loader2 className="spin" size={15} />
          ) : (
            <Save size={15} />
          )}
          <span>Apply</span>
        </button>
        {actionStatus ? (
          <div
            className={`config-action-status config-action-status-${actionStatus.tone}`}
            role={actionStatus.tone === "fail" ? "alert" : "status"}
          >
            {actionStatus.tone === "pending" ? (
              <Loader2 className="spin" size={15} aria-hidden="true" />
            ) : actionStatus.tone === "fail" ? (
              <AlertTriangle size={15} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={15} aria-hidden="true" />
            )}
            <span>{actionStatus.message}</span>
            {traceId && actionStatus.tone === "fail" ? (
              <code>{traceId}</code>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function productConfigActionStatus({
  submitting,
  panelError,
  successMessage,
}: {
  submitting: ProductConfigApplyRequest["mode"] | null;
  panelError: string;
  successMessage: string;
}): { tone: "pending" | "pass" | "fail"; message: string } | null {
  if (submitting === "dry-run") {
    return { tone: "pending", message: "Running dry run..." };
  }
  if (submitting === "apply") {
    return { tone: "pending", message: "Applying product config..." };
  }
  if (panelError) {
    return { tone: "fail", message: panelError };
  }
  if (successMessage) {
    return { tone: "pass", message: successMessage };
  }
  return null;
}

function productConfigRequestErrorMessage(
  error: Error,
  phase: "dry run" | "apply",
): string {
  if (error.name === "AbortError") {
    return `Product config ${phase} timed out. The request may still have reached Launchplane; refresh status before retrying.`;
  }
  return error.message;
}

function applySuccessMessage(result: ProductConfigApplyPayload): string {
  if (result.status === "records_applied_live_sync_required") {
    return "Apply completed. Launchplane records are current; live runtime sync is still required.";
  }
  if (
    result.runtime_environment.action === "unchanged" &&
    result.summary.secret_change_count === 0
  ) {
    return "Apply completed. No record changes were needed.";
  }
  return "Apply completed. Launchplane records are current.";
}

function ProductConfigResult({
  result,
}: {
  result: ProductConfigApplyPayload;
}) {
  const runtime = result.runtime_environment;
  return (
    <div className="config-result" aria-live="polite">
      <div className="config-result-summary">
        <KeyValue
          label="Mode"
          value={result.mode}
          status={result.mode === "apply" ? "pass" : "pending"}
        />
        <KeyValue label="Actor" value={result.actor} mono />
        <KeyValue
          label="Runtime"
          value={`${runtime.action} / ${result.summary.runtime_changed_key_count} changed`}
          status={runtime.action === "unchanged" ? "unknown" : "pass"}
        />
        <KeyValue
          label="Secrets"
          value={`${result.summary.secret_change_count} changed`}
          status={result.summary.secret_change_count ? "pass" : "unknown"}
        />
      </div>
      <div className="config-result-list">
        {result.next_actions?.length ? (
          <div className="config-result-row config-result-row-note">
            <strong>Next action</strong>
            <span>
              {result.next_actions[0].instruction ??
                "Live runtime sync required."}
            </span>
          </div>
        ) : null}
        {runtime.keys.length ? (
          <div className="config-result-row">
            <strong>Runtime keys</strong>
            <code>{runtime.keys.join(", ")}</code>
          </div>
        ) : null}
        {result.secrets.map((secret) => (
          <div
            className="config-result-row"
            key={`${secret.secret_id}:${secret.binding_key}`}
          >
            <strong>{secret.binding_key}</strong>
            <code>
              {secret.action} / {secret.secret_id}
            </code>
          </div>
        ))}
      </div>
    </div>
  );
}

function newRuntimeConfigRow(): RuntimeConfigRow {
  return { id: clientId(), key: "", value: "" };
}

function newSecretConfigRow(): SecretConfigRow {
  return {
    id: clientId(),
    name: "",
    bindingKey: "",
    value: "",
    description: "",
  };
}

function clientId(): string {
  return window.crypto.randomUUID?.() ?? Math.random().toString(36).slice(2);
}

export function runtimeScopeForTarget(
  contextName: string,
  instanceName: string,
): ProductConfigRuntimeScope {
  if (contextName.trim() && instanceName.trim()) {
    return "instance";
  }
  if (contextName.trim()) {
    return "context";
  }
  return "global";
}

export function secretScopeForTarget(
  contextName: string,
  instanceName: string,
): ProductConfigSecretScope {
  if (contextName.trim() && instanceName.trim()) {
    return "context_instance";
  }
  if (contextName.trim()) {
    return "context";
  }
  return "global";
}
