import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Eye,
  KeyRound,
  LoaderCircle,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { applyProductEnvironmentConfig } from "./api";
import {
  beginBrowserOperation,
  cancelBrowserOperation,
  completeBrowserOperation,
  createBrowserOperationState,
  failBrowserOperation,
  markBrowserOperationDispatched,
  prepareBrowserOperation,
  resetBrowserOperation,
  retryBrowserOperation,
  type BrowserOperationOptions,
  type BrowserOperationState,
} from "./browser-operation";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import {
  clearManagedSecretInputs,
  consumeManagedSecretValues,
  productConfigDraftLocked,
  productConfigFailureCertainty,
  productConfigManagedSecretIdentity,
  productConfigOperationFailure,
  productConfigRuntimeDraftKey,
  productConfigSelectionKey,
} from "./product-config-operation";

import type {
  ApplyProductEnvironmentConfigData,
  ProductConfigApplyResponse,
  ProductConfigInputWriteAvailability,
  ProductEnvironmentConfigStatus,
  ProductManagedSecretConfigStatusItem,
  ProductRuntimeConfigStatusItem,
} from "./generated/openapi.ts";

type EnvironmentConfigRequest = ApplyProductEnvironmentConfigData["body"];

interface ProductConfigFormProps {
  config: ProductEnvironmentConfigStatus;
  fixtureMode: DevFixtureMode;
  onApplied: () => void;
}

interface ProductConfigOperationController {
  reset: () => boolean;
  run: (payload: EnvironmentConfigRequest) => Promise<ProductConfigApplyResponse | null>;
  state: BrowserOperationState;
}

export function RuntimeSettingsChangePanel({
  config,
  fixtureMode,
  onApplied,
}: ProductConfigFormProps) {
  const availability = config.write_availability.runtime_settings;
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState("");
  const [planResult, setPlanResult] = useState<ProductConfigApplyResponse | null>(null);
  const [applyResult, setApplyResult] = useState<ProductConfigApplyResponse | null>(null);
  const [plannedDraftKey, setPlannedDraftKey] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const planOperation = useProductConfigOperation(
    `${config.product}:${config.environment}:runtime-settings:plan`,
    config.product,
    config.environment,
    fixtureMode,
  );
  const applyOperation = useProductConfigOperation(
    `${config.product}:${config.environment}:runtime-settings:apply`,
    config.product,
    config.environment,
    fixtureMode,
  );
  const draftKey = productConfigRuntimeDraftKey(selectedKeys, values);
  const planMatchesDraft = Boolean(planResult && plannedDraftKey === draftKey);
  const operationBusy = isOperationBusy(planOperation.state) || isOperationBusy(applyOperation.state);
  const draftLocked = productConfigDraftLocked(planOperation.state, applyOperation.state);

  async function planChanges() {
    setLocalError("");
    if (!reason.trim()) {
      setLocalError("Enter a reason before running the dry-run.");
      return;
    }
    if (!selectedKeys.length) {
      setLocalError("Select at least one runtime setting.");
      return;
    }
    if (applyOperation.state.requiresIdempotencyContinuity) {
      setLocalError("Resolve the uncertain apply before creating a new plan.");
      return;
    }
    applyOperation.reset();
    setApplyResult(null);
    setPlanResult(null);
    setPlannedDraftKey("");
    setConfirmed(false);
    const runtimeSettings = Object.fromEntries(
      selectedKeys.map((key) => [key, values[key] ?? ""]),
    );
    const response = await planOperation.run({
      schema_version: 1,
      mode: "dry-run",
      reason: reason.trim(),
      runtime_settings: runtimeSettings,
    });
    if (response) {
      setPlanResult(response);
      setPlannedDraftKey(draftKey);
    }
  }

  async function applyChanges() {
    setLocalError("");
    if (!planMatchesDraft) {
      setLocalError("Runtime settings changed after the dry-run. Run a new dry-run first.");
      return;
    }
    if (!reason.trim()) {
      setLocalError("Enter an apply reason.");
      return;
    }
    if (!confirmed) {
      setLocalError("Confirm the reviewed scope and consequences before apply.");
      return;
    }
    const runtimeSettings = Object.fromEntries(
      selectedKeys.map((key) => [key, values[key] ?? ""]),
    );
    setApplyResult(null);
    const response = await applyOperation.run({
      schema_version: 1,
      mode: "apply",
      reason: reason.trim(),
      confirmation: availability.apply.confirmation_text,
      runtime_settings: runtimeSettings,
    });
    if (response) {
      setApplyResult(response);
      setConfirmed(false);
      onApplied();
    }
  }

  function startOver() {
    if (!planOperation.reset() || !applyOperation.reset()) {
      setLocalError("An uncertain apply must be retried with its existing operation key.");
      return;
    }
    setSelectedKeys([]);
    setValues({});
    setReason("");
    setLocalError("");
    setPlanResult(null);
    setApplyResult(null);
    setPlannedDraftKey("");
    setConfirmed(false);
  }

  return (
    <ProductConfigPanel
      availability={availability}
      description="Select only the declared keys you intend to change. Existing values stay server-side and are never loaded into this form."
      icon={<ClipboardCheck />}
      title="Plan runtime setting changes"
    >
      <AvailabilityBlockers availability={availability} />
      <fieldset disabled={!availability.plan.enabled || draftLocked}>
        <legend className="sr-only">Runtime settings to change</legend>
        <div className="product-config-fields">
          {config.runtime_settings.map((setting) => (
            <RuntimeSettingInput
              key={setting.key}
              selected={selectedKeys.includes(setting.key)}
              setting={setting}
              value={values[setting.key] ?? ""}
              onSelected={(selected) => {
                setLocalError("");
                setPlanResult(null);
                setConfirmed(false);
                setSelectedKeys((current) =>
                  selected
                    ? [...new Set([...current, setting.key])].sort()
                    : current.filter((key) => key !== setting.key),
                );
              }}
              onValue={(value) => {
                setLocalError("");
                setPlanResult(null);
                setConfirmed(false);
                setValues((current) => ({ ...current, [setting.key]: value }));
              }}
            />
          ))}
        </div>
        <ReasonField reason={reason} onChange={setReason} />
      </fieldset>
      <OperationNotice state={planOperation.state} label="Dry-run" />
      <OperationNotice state={applyOperation.state} label="Apply" />
      {localError ? <InlineFormError message={localError} /> : null}
      <div className="product-config-actions">
        <button
          className="button"
          disabled={!availability.plan.enabled || draftLocked || !selectedKeys.length}
          onClick={() => void planChanges()}
          type="button"
        >
          {isOperationBusy(planOperation.state) ? <LoaderCircle className="spin" /> : <Eye />}
          Run dry-run
        </button>
        {(planResult || applyResult) && !applyOperation.state.requiresIdempotencyContinuity ? (
          <button className="button" onClick={startOver} type="button">
            <RotateCcw />
            Start over
          </button>
        ) : null}
      </div>
      {planResult ? (
        <ProductConfigEvidence label="Dry-run evidence" response={planResult} state={planOperation.state} />
      ) : null}
      {planResult && !planMatchesDraft ? (
        <InlineFormError message="The draft changed after this dry-run. Apply is disabled until you run it again." />
      ) : null}
      {planResult && planMatchesDraft && !applyResult ? (
        <ApplyConfirmation
          availability={availability}
          confirmed={confirmed}
          disabled={!availability.apply.enabled || operationBusy}
          onApply={() => void applyChanges()}
          onConfirmed={setConfirmed}
          reason={reason}
          response={planResult}
          retrying={applyOperation.state.requiresIdempotencyContinuity}
        />
      ) : null}
      {applyResult ? (
        <ProductConfigEvidence label="Apply evidence" response={applyResult} state={applyOperation.state} />
      ) : null}
    </ProductConfigPanel>
  );
}

export function ManagedSecretsChangePanel({
  config,
  fixtureMode,
  onApplied,
}: ProductConfigFormProps) {
  const availability = config.write_availability.managed_secrets;
  const [selectedIdentities, setSelectedIdentities] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState("");
  const [planResult, setPlanResult] = useState<ProductConfigApplyResponse | null>(null);
  const [applyResult, setApplyResult] = useState<ProductConfigApplyResponse | null>(null);
  const [plannedSelectionKey, setPlannedSelectionKey] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const secretInputs = useRef(new Map<string, HTMLInputElement>());
  const planOperation = useProductConfigOperation(
    `${config.product}:${config.environment}:managed-secrets:plan`,
    config.product,
    config.environment,
    fixtureMode,
  );
  const applyOperation = useProductConfigOperation(
    `${config.product}:${config.environment}:managed-secrets:apply`,
    config.product,
    config.environment,
    fixtureMode,
  );
  const secretsByIdentity = new Map(
    config.managed_secrets.map((secret) => [
      productConfigManagedSecretIdentity(secret.integration, secret.binding_key),
      secret,
    ]),
  );
  const selectedSecrets = selectedIdentities.flatMap((identity) => {
    const secret = secretsByIdentity.get(identity);
    return secret
      ? [{ bindingKey: secret.binding_key, integration: secret.integration, identity }]
      : [];
  });
  const selectionKey = productConfigSelectionKey(selectedIdentities);
  const planMatchesSelection = Boolean(planResult && selectionKey === plannedSelectionKey);
  const operationBusy = isOperationBusy(planOperation.state) || isOperationBusy(applyOperation.state);
  const draftLocked = productConfigDraftLocked(planOperation.state, applyOperation.state);

  useEffect(
    () => () => {
      clearManagedSecretInputs(secretInputs.current);
    },
    [],
  );

  async function planChanges() {
    setLocalError("");
    if (!reason.trim()) {
      setLocalError("Enter a reason before running the dry-run.");
      return;
    }
    if (!selectedIdentities.length) {
      setLocalError("Select at least one managed-secret binding.");
      return;
    }
    if (applyOperation.state.requiresIdempotencyContinuity) {
      setLocalError("Resolve the uncertain apply before creating a new plan.");
      return;
    }
    let managedSecrets;
    try {
      managedSecrets = consumeManagedSecretValues(selectedSecrets, secretInputs.current);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "Secret input validation failed.");
      return;
    }
    applyOperation.reset();
    setApplyResult(null);
    setPlanResult(null);
    setPlannedSelectionKey("");
    setConfirmed(false);
    const response = await planOperation.run({
      schema_version: 1,
      mode: "dry-run",
      reason: reason.trim(),
      managed_secrets: managedSecrets,
    });
    clearManagedSecretInputs(secretInputs.current);
    if (response) {
      setPlanResult(response);
      setPlannedSelectionKey(selectionKey);
    }
  }

  async function applyChanges() {
    setLocalError("");
    if (!planMatchesSelection) {
      setLocalError("Secret bindings changed after the dry-run. Run a new dry-run first.");
      return;
    }
    if (!reason.trim()) {
      setLocalError("Enter an apply reason.");
      return;
    }
    if (!confirmed) {
      setLocalError("Confirm the reviewed scope and consequences before apply.");
      return;
    }
    let managedSecrets;
    try {
      managedSecrets = consumeManagedSecretValues(selectedSecrets, secretInputs.current);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "Secret input validation failed.");
      return;
    }
    setApplyResult(null);
    const response = await applyOperation.run({
      schema_version: 1,
      mode: "apply",
      reason: reason.trim(),
      confirmation: availability.apply.confirmation_text,
      managed_secrets: managedSecrets,
    });
    clearManagedSecretInputs(secretInputs.current);
    if (response) {
      setApplyResult(response);
      setConfirmed(false);
      onApplied();
    }
  }

  function startOver() {
    clearManagedSecretInputs(secretInputs.current);
    if (!planOperation.reset() || !applyOperation.reset()) {
      setLocalError("An uncertain apply must be retried with its existing operation key.");
      return;
    }
    setSelectedIdentities([]);
    setReason("");
    setLocalError("");
    setPlanResult(null);
    setApplyResult(null);
    setPlannedSelectionKey("");
    setConfirmed(false);
  }

  return (
    <ProductConfigPanel
      availability={availability}
      description="Select declared bindings, enter each value only for the immediate request, and re-enter values after dry-run. Plaintext is cleared before the network request begins."
      icon={<KeyRound />}
      title="Plan managed-secret changes"
    >
      <AvailabilityBlockers availability={availability} />
      <fieldset disabled={!availability.plan.enabled || draftLocked}>
        <legend className="sr-only">Managed secrets to change</legend>
        <div className="product-config-fields">
          {config.managed_secrets.map((secret) => (
            <ManagedSecretInput
              key={`${secret.integration}:${secret.binding_key}`}
              inputRef={(element) => {
                const identity = productConfigManagedSecretIdentity(
                  secret.integration,
                  secret.binding_key,
                );
                if (element) {
                  secretInputs.current.set(identity, element);
                } else {
                  secretInputs.current.delete(identity);
                }
              }}
              secret={secret}
              selected={selectedIdentities.includes(
                productConfigManagedSecretIdentity(secret.integration, secret.binding_key),
              )}
              onSelected={(selected) => {
                const identity = productConfigManagedSecretIdentity(
                  secret.integration,
                  secret.binding_key,
                );
                const input = secretInputs.current.get(identity);
                if (!selected && input) {
                  input.value = "";
                }
                setLocalError("");
                setPlanResult(null);
                setConfirmed(false);
                setSelectedIdentities((current) =>
                  selected
                    ? [...new Set([...current, identity])].sort()
                    : current.filter((currentIdentity) => currentIdentity !== identity),
                );
              }}
            />
          ))}
        </div>
        <ReasonField reason={reason} onChange={setReason} />
      </fieldset>
      <div className="secret-entry-rule">
        <ShieldCheck aria-hidden="true" />
        <p>
          Values are write-only. This browser clears every password field on submit,
          secret-input validation error, route change, and unmount.
        </p>
      </div>
      <OperationNotice state={planOperation.state} label="Dry-run" />
      <OperationNotice state={applyOperation.state} label="Apply" />
      {localError ? <InlineFormError message={localError} /> : null}
      <div className="product-config-actions">
        <button
          className="button"
          disabled={
            !availability.plan.enabled || draftLocked || !selectedIdentities.length
          }
          onClick={() => void planChanges()}
          type="button"
        >
          {isOperationBusy(planOperation.state) ? <LoaderCircle className="spin" /> : <Eye />}
          Run dry-run
        </button>
        {(planResult || applyResult) && !applyOperation.state.requiresIdempotencyContinuity ? (
          <button className="button" onClick={startOver} type="button">
            <RotateCcw />
            Start over
          </button>
        ) : null}
      </div>
      {planResult ? (
        <>
          <ProductConfigEvidence label="Dry-run evidence" response={planResult} state={planOperation.state} />
          <div className="secret-reentry-note">
            <KeyRound aria-hidden="true" />
            <p>
              Re-enter the same values before apply. Launchplane retained only the
              reviewed digest and redacted plan evidence.
            </p>
          </div>
        </>
      ) : null}
      {planResult && !planMatchesSelection ? (
        <InlineFormError message="The selected bindings changed after this dry-run. Apply is disabled until you run it again." />
      ) : null}
      {planResult && planMatchesSelection && !applyResult ? (
        <ApplyConfirmation
          availability={availability}
          confirmed={confirmed}
          disabled={!availability.apply.enabled || operationBusy}
          onApply={() => void applyChanges()}
          onConfirmed={setConfirmed}
          reason={reason}
          response={planResult}
          retrying={applyOperation.state.requiresIdempotencyContinuity}
        />
      ) : null}
      {applyResult ? (
        <ProductConfigEvidence label="Apply evidence" response={applyResult} state={applyOperation.state} />
      ) : null}
    </ProductConfigPanel>
  );
}

function ProductConfigPanel({
  availability,
  children,
  description,
  icon,
  title,
}: {
  availability: ProductConfigInputWriteAvailability;
  children: ReactNode;
  description: string;
  icon: ReactNode;
  title: string;
}) {
  return (
    <section className="product-config-panel">
      <header className="product-config-panel-header">
        <span aria-hidden="true">{icon}</span>
        <div>
          <p className="eyebrow">Controlled change</p>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <span className="availability-state" data-enabled={availability.plan.enabled}>
          {availability.plan.enabled ? "Plan available" : "Blocked"}
        </span>
      </header>
      {children}
    </section>
  );
}

function RuntimeSettingInput({
  onSelected,
  onValue,
  selected,
  setting,
  value,
}: {
  onSelected: (selected: boolean) => void;
  onValue: (value: string) => void;
  selected: boolean;
  setting: ProductRuntimeConfigStatusItem;
  value: string;
}) {
  const inputId = `runtime-setting-${setting.key}`;
  return (
    <div className="product-config-field" data-selected={selected}>
      <label className="product-config-select" htmlFor={`${inputId}-selected`}>
        <input
          checked={selected}
          id={`${inputId}-selected`}
          onChange={(event) => onSelected(event.target.checked)}
          type="checkbox"
        />
        <span>
          <strong>{setting.key}</strong>
          <small>{setting.status} · {setting.trust_state}</small>
        </span>
      </label>
      <label htmlFor={inputId}>
        New value
        <input
          autoComplete="off"
          disabled={!selected}
          id={inputId}
          onChange={(event) => onValue(event.target.value)}
          placeholder="Enter a replacement value"
          type="text"
          value={value}
        />
      </label>
    </div>
  );
}

function ManagedSecretInput({
  inputRef,
  onSelected,
  secret,
  selected,
}: {
  inputRef: (element: HTMLInputElement | null) => void;
  onSelected: (selected: boolean) => void;
  secret: ProductManagedSecretConfigStatusItem;
  selected: boolean;
}) {
  const inputId = `managed-secret-${secret.binding_key}`;
  return (
    <div className="product-config-field" data-selected={selected}>
      <label className="product-config-select" htmlFor={`${inputId}-selected`}>
        <input
          checked={selected}
          id={`${inputId}-selected`}
          onChange={(event) => onSelected(event.target.checked)}
          type="checkbox"
        />
        <span>
          <strong>{secret.binding_key}</strong>
          <small>{secret.integration} · {secret.status}</small>
        </span>
      </label>
      <label htmlFor={inputId}>
        Write-only value
        <input
          autoComplete="new-password"
          disabled={!selected}
          id={inputId}
          placeholder="Value is cleared on submit"
          ref={inputRef}
          type="password"
        />
      </label>
    </div>
  );
}

function ReasonField({
  onChange,
  reason,
}: {
  onChange: (reason: string) => void;
  reason: string;
}) {
  return (
    <label className="product-config-reason">
      Change reason
      <textarea
        maxLength={500}
        onChange={(event) => onChange(event.target.value)}
        placeholder="Why is this change required now?"
        rows={3}
        value={reason}
      />
      <small>This reason is shown in dry-run and apply evidence.</small>
    </label>
  );
}

function AvailabilityBlockers({
  availability,
}: {
  availability: ProductConfigInputWriteAvailability;
}) {
  const planningBlocked = availability.plan.disabled_reasons.length > 0;
  const blockers = [
    ...new Set(
      planningBlocked
        ? availability.plan.disabled_reasons
        : availability.apply.disabled_reasons,
    ),
  ];
  if (!blockers.length) {
    return null;
  }
  return (
    <div className="product-config-blockers" role="status">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{planningBlocked ? "Configuration planning is blocked" : "Apply remains blocked"}</strong>
        <ul>
          {blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
      </div>
    </div>
  );
}

function ApplyConfirmation({
  availability,
  confirmed,
  disabled,
  onApply,
  onConfirmed,
  reason,
  response,
  retrying,
}: {
  availability: ProductConfigInputWriteAvailability;
  confirmed: boolean;
  disabled: boolean;
  onApply: () => void;
  onConfirmed: (confirmed: boolean) => void;
  reason: string;
  response: ProductConfigApplyResponse;
  retrying: boolean;
}) {
  const result = response.result;
  return (
    <section className="product-config-confirmation" aria-label="Apply confirmation">
      <div className="confirmation-heading">
        <ShieldCheck aria-hidden="true" />
        <div>
          <p className="eyebrow">Explicit confirmation</p>
          <h3>Confirm the reviewed change</h3>
        </div>
      </div>
      <dl>
        <div><dt>Scope</dt><dd>{result.product} / {result.instance}</dd></div>
        <div><dt>Reason</dt><dd>{reason.trim() || "Reason required"}</dd></div>
        <div><dt>Runtime keys</dt><dd>{result.summary.runtime_changed_key_count}</dd></div>
        <div><dt>Secret bindings</dt><dd>{result.summary.secret_change_count}</dd></div>
      </dl>
      <ul className="confirmation-consequences">
        {availability.consequences.map((consequence) => (
          <li key={consequence}>{consequence}</li>
        ))}
        {result.next_actions.map((nextAction) => (
          <li key={`${nextAction.kind}:${nextAction.target.target_name}`}>
            Live synchronization remains required for {nextAction.target.target_name}.
          </li>
        ))}
      </ul>
      <label className="confirmation-check">
        <input
          checked={confirmed}
          disabled={disabled}
          onChange={(event) => onConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>
          I reviewed the scope, changed key counts, irreversible consequences, and any
          separate live-sync step.
        </span>
      </label>
      <button
        className="button button-primary"
        disabled={disabled || !confirmed}
        onClick={onApply}
        type="button"
      >
        {disabled ? <LoaderCircle className="spin" /> : <CheckCircle2 />}
        {retrying ? "Retry same apply" : "Apply reviewed change"}
      </button>
    </section>
  );
}

function ProductConfigEvidence({
  label,
  response,
  state,
}: {
  label: string;
  response: ProductConfigApplyResponse;
  state: BrowserOperationState;
}) {
  const result = response.result;
  const changedKeys = result.runtime_environment.changed_keys;
  return (
    <section className="product-config-evidence" aria-label={label}>
      <header>
        <span><ClipboardCheck aria-hidden="true" /></span>
        <div>
          <p className="eyebrow">{label}</p>
          <h3>{result.mode === "apply" ? "Configuration recorded" : "Change plan ready"}</h3>
        </div>
        <span className="evidence-outcome" data-status={result.status}>
          {response.replayed ? "Replay confirmed" : result.status.replaceAll("_", " ")}
        </span>
      </header>
      <dl>
        <div><dt>Scope</dt><dd>{result.product} / {result.instance}</dd></div>
        <div><dt>Runtime changes</dt><dd>{result.summary.runtime_changed_key_count}</dd></div>
        <div><dt>Secret changes</dt><dd>{result.summary.secret_change_count}</dd></div>
        <div><dt>Key safety</dt><dd>{result.runtime_key_safety.status}</dd></div>
      </dl>
      {changedKeys.length ? (
        <div className="evidence-key-list">
          <strong>Changed runtime keys</strong>
          <ul>{changedKeys.map((key) => <li key={key}><code>{key}</code></li>)}</ul>
        </div>
      ) : null}
      {result.secrets.length ? (
        <div className="evidence-key-list">
          <strong>Managed-secret actions</strong>
          <ul>
            {result.secrets.map((secret) => (
              <li key={`${secret.integration}:${secret.binding_key}`}>
                <code>{secret.binding_key}</code> · {secret.action}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {result.next_actions.length ? (
        <div className="inspect-only-actions">
          <strong>Inspect-only next actions</strong>
          {result.next_actions.map((nextAction) => (
            <div key={`${nextAction.kind}:${nextAction.target.target_name}`}>
              <span><Eye aria-hidden="true" /> Separate typed operation required</span>
              <p>{nextAction.instruction}</p>
              <code>{nextAction.dry_run.method} {nextAction.dry_run.endpoint}</code>
            </div>
          ))}
        </div>
      ) : null}
      <footer>
        <span>Trace <code>{state.receipt?.traceId || response.trace_id}</code></span>
        {state.receipt?.originalTraceId || response.original_trace_id ? (
          <span>Original <code>{state.receipt?.originalTraceId || response.original_trace_id}</code></span>
        ) : null}
      </footer>
    </section>
  );
}

function OperationNotice({
  label,
  state,
}: {
  label: string;
  state: BrowserOperationState;
}) {
  if (!state.failure) {
    if (state.phase !== "uncertain") {
      return null;
    }
  }
  return (
    <div className="operation-notice" data-phase={state.phase} role="status">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{label} {state.phase}</strong>
        <p>{state.failure?.message || "The result is uncertain. Retry only with the existing operation key."}</p>
        {state.failure?.code ? <code>{state.failure.code}</code> : null}
        {state.failure?.traceId ? <small>Trace {state.failure.traceId}</small> : null}
      </div>
    </div>
  );
}

function InlineFormError({ message }: { message: string }) {
  return (
    <div className="product-config-inline-error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function useProductConfigOperation(
  scope: string,
  product: string,
  environment: string,
  fixtureMode: DevFixtureMode,
): ProductConfigOperationController {
  const [state, setState] = useState<BrowserOperationState>(createBrowserOperationState());
  const stateRef = useRef(state);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  function updateState(next: BrowserOperationState) {
    stateRef.current = next;
    if (mountedRef.current) {
      setState(next);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  async function execute(
    payload: EnvironmentConfigRequest,
    options: BrowserOperationOptions,
  ): Promise<ProductConfigApplyResponse> {
    if (fixtureMode) {
      const fixtures = await loadDevFixtures();
      options.onDispatch?.();
      return fixtures.applyProductEnvironmentConfigForFixture(
        fixtureMode,
        product,
        environment,
        payload,
        options.signal,
      );
    }
    return applyProductEnvironmentConfig(product, environment, payload, options);
  }

  async function run(payload: EnvironmentConfigRequest) {
    let current = stateRef.current;
    if (current.phase === "succeeded") {
      current = resetBrowserOperation(current);
    } else if (["failed", "uncertain", "cancelled"].includes(current.phase)) {
      current = retryBrowserOperation(current);
    }
    try {
      current = await prepareBrowserOperation(scope, payload, current);
      current = beginBrowserOperation(current);
      updateState(current);
    } catch (error) {
      updateState({
        ...current,
        failure: productConfigOperationFailure(error),
        phase: current.requiresIdempotencyContinuity ? "uncertain" : "failed",
      });
      return null;
    }

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    let dispatched = false;
    try {
      const response = await execute(payload, {
        idempotencyKey: current.identity?.idempotencyKey ?? "",
        signal: controller.signal,
        onDispatch: () => {
          dispatched = true;
          current = markBrowserOperationDispatched(current);
          updateState(current);
        },
      });
      if (!dispatched) {
        current = markBrowserOperationDispatched(current);
      }
      current = completeBrowserOperation(current, response);
      updateState(current);
      return response;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        current = cancelBrowserOperation(current);
      } else {
        current = failBrowserOperation(
          current,
          productConfigOperationFailure(error),
          productConfigFailureCertainty(error, dispatched),
        );
      }
      updateState(current);
      return null;
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }

  function reset() {
    try {
      updateState(resetBrowserOperation(stateRef.current));
      return true;
    } catch {
      return false;
    }
  }

  return { reset, run, state };
}

function isOperationBusy(state: BrowserOperationState): boolean {
  return state.phase === "queued" || state.phase === "submitting";
}
