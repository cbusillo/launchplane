import {
  AlertTriangle,
  Ban,
  Database,
  LoaderCircle,
  Search,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";
import { Fragment, useEffect, useRef, useState } from "react";

import {
  LaunchplaneApiError,
  readOrdinaryAgentDeliveryAuthorizationCandidateInputs,
  type OrdinaryAgentDeliveryInspectionRuntimeState,
  type OrdinaryAgentDeliveryInspectionSecretState,
  type OrdinaryAgentDeliveryInspectionSetup,
  type OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { formatTime } from "./format";
import { EngineeringOrdinaryAgentPolicyPreparation } from "./EngineeringOrdinaryAgentPolicyPreparation";

type CheckPhase = "idle" | "loading" | "ready" | "denied" | "error" | "cancelled";

interface CheckState {
  phase: CheckPhase;
  data: OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse | null;
  message: string;
  traceId: string;
}

const INITIAL_STATE: CheckState = {
  phase: "idle",
  data: null,
  message: "",
  traceId: "",
};

export function EngineeringOrdinaryAgentPreparationInputs({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const [state, setState] = useState<CheckState>(INITIAL_STATE);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  useEffect(
    () => () => {
      requestIdRef.current += 1;
      controllerRef.current?.abort();
    },
    [],
  );

  async function checkPrerequisites() {
    controllerRef.current?.abort();
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    controllerRef.current = controller;
    setState({ ...INITIAL_STATE, phase: "loading" });
    try {
      const requestedMode = new URLSearchParams(window.location.search).get(
        "preparation",
      );
      const data = fixtureMode && requestedMode !== "api"
        ? await preparationInputsFixture(fixtureMode, controller.signal)
        : await readOrdinaryAgentDeliveryAuthorizationCandidateInputs(
            controller.signal,
          );
      if (requestIdRef.current !== requestId || controller.signal.aborted) return;
      setState({ phase: "ready", data, message: "", traceId: "" });
    } catch (error) {
      if (requestIdRef.current !== requestId) return;
      if (controller.signal.aborted || isAbortError(error)) {
        setState({ ...INITIAL_STATE, phase: "cancelled" });
        return;
      }
      if (error instanceof LaunchplaneApiError) {
        setState({
          phase:
            error.statusCode === 401 || error.statusCode === 403
              ? "denied"
              : "error",
          data: null,
          message: error.message,
          traceId: error.traceId,
        });
        return;
      }
      setState({
        ...INITIAL_STATE,
        phase: "error",
        message: "Launchplane could not read the current setup prerequisites.",
      });
    } finally {
      if (requestIdRef.current === requestId) controllerRef.current = null;
    }
  }

  return (
    <section
      className="ordinary-agent-preparation-inputs"
      aria-labelledby="ordinary-agent-preparation-inputs-title"
    >
      <header>
        <div>
          <span className="engineering-kicker">Setup prerequisites</span>
          <h3 id="ordinary-agent-preparation-inputs-title">
            Check current configuration
          </h3>
          <p>
            See which repositories and branches Launchplane has recorded for
            setup.
          </p>
        </div>
        <button
          className="button"
          disabled={state.phase === "loading"}
          onClick={() => void checkPrerequisites()}
          type="button"
        >
          {state.phase === "loading" ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <Search size={16} aria-hidden="true" />
          )}
          {state.phase === "loading"
            ? "Checking setup prerequisites…"
            : state.phase === "idle"
              ? "Check setup prerequisites"
              : "Check setup prerequisites again"}
        </button>
      </header>

      <p className="ordinary-agent-preparation-boundary">
        This check does not inspect agent registration or preview readiness.
      </p>

      {state.phase === "idle" ? (
        <p className="ordinary-agent-preparation-prompt">
          Run the check when you need a fresh read. It does not prepare a plan
          or change setup.
        </p>
      ) : null}
      {state.phase === "loading" ? (
        <div className="ordinary-agent-preparation-state" role="status">
          <LoaderCircle className="spin" size={19} aria-hidden="true" />
          <span>Reading current setup prerequisites…</span>
        </div>
      ) : null}
      {state.phase === "denied" ? (
        <PreparationFailure
          icon={ShieldAlert}
          message={
            state.message ||
            "This browser session cannot read setup prerequisites."
          }
          title="Setup-prerequisite access denied"
          traceId={state.traceId}
        />
      ) : null}
      {state.phase === "error" ? (
        <PreparationFailure
          icon={AlertTriangle}
          message={
            state.message ||
            "Launchplane could not read setup prerequisites."
          }
          title="Setup prerequisites unavailable"
          traceId={state.traceId}
        />
      ) : null}
      {state.phase === "cancelled" ? (
        <PreparationFailure
          icon={Ban}
          message="The setup-prerequisite request was cancelled before a response was accepted."
          title="Check cancelled"
        />
      ) : null}
      {state.phase === "ready" && state.data ? (
        <PreparationInputsResult data={state.data} />
      ) : null}
    </section>
  );
}

function PreparationInputsResult({
  data,
}: {
  data: OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse;
}) {
  const inspectionSetup = data.inspection_setup;
  const incompleteInventory = data.inventory_state !== "complete";
  const mergePolicyAvailable = data.merge_policy_state === "available";

  return (
    <div className="ordinary-agent-preparation-result">
      <EngineeringOrdinaryAgentPolicyPreparation data={data} />
      <dl className="ordinary-agent-preparation-summary">
        <div>
          <dt>Repository inventory</dt>
          <dd data-state={data.inventory_state}>
            {displayState(data.inventory_state)}
          </dd>
        </div>
        <div>
          <dt>Merge policy</dt>
          <dd data-state={data.merge_policy_state}>
            {displayState(data.merge_policy_state)}
          </dd>
        </div>
      </dl>

      <InspectionSetupResult inspectionSetup={inspectionSetup} />

      {incompleteInventory ? (
        <div className="ordinary-agent-preparation-state" role="status">
          <AlertTriangle size={19} aria-hidden="true" />
          <span>
            The repository inventory is{" "}
            {displayState(data.inventory_state).toLowerCase()}.
            Any repositories shown below are only the returned records.
          </span>
        </div>
      ) : null}
      {!mergePolicyAvailable ? (
        <div className="ordinary-agent-preparation-state" role="status">
          <AlertTriangle size={19} aria-hidden="true" />
          <span>
            The merge policy is{" "}
            {displayState(data.merge_policy_state).toLowerCase()}.
          </span>
        </div>
      ) : null}

      {data.repositories.length ? (
        <ul
          className="ordinary-agent-preparation-repositories"
          aria-label="Configured repositories"
        >
          {data.repositories.map((repository) => (
            <li key={repository.record_id}>
              <header>
                <strong>{repository.repository}</strong>
                <span>Tracked</span>
              </header>
              <div>
                <span>Configured branches</span>
                {mergePolicyAvailable && repository.configured_branches.length ? (
                  <ul>
                    {repository.configured_branches.map((branch) => (
                      <li key={branch}>
                        <code>{branch}</code>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <strong className="ordinary-agent-missing-branches">
                    {mergePolicyAvailable
                      ? "Missing branch configuration"
                      : "Branch configuration not verified"}
                  </strong>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="ordinary-agent-preparation-state" role="status">
          <Database size={19} aria-hidden="true" />
          <span>No configured repositories were returned.</span>
        </div>
      )}

      {data.diagnostics.length ? (
        <ul
          className="ordinary-agent-preparation-diagnostics"
          aria-label="Setup-prerequisite diagnostics"
        >
          {data.diagnostics.map((diagnostic) => (
            <li key={`${diagnostic.code}:${diagnostic.message}`}>
              <span>{diagnostic.message}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <details className="privileged-operation-policy-review">
        <summary>Technical provenance</summary>
        <dl className="privileged-operation-details ordinary-agent-preparation-provenance">
          <ProvenanceValue label="Trace ID" value={data.trace_id} />
          <ProvenanceValue
            label="Observed at"
            value={formatTime(data.observed_at)}
          />
          <ProvenanceValue
            label="Authorization policy record ID"
            value={data.authorization_policy.record_id}
          />
          <ProvenanceValue
            label="Authorization policy revision"
            value={String(data.authorization_policy.revision)}
          />
          <ProvenanceValue
            label="Authorization policy schema version"
            value={String(data.authorization_policy.schema_version)}
          />
          <ProvenanceValue
            code
            label="Authorization policy digest"
            value={data.authorization_policy.policy_sha256}
          />
          {data.merge_policy ? (
            <>
              <ProvenanceValue
                label="Merge policy record ID"
                value={data.merge_policy.record_id}
              />
              <ProvenanceValue
                label="Merge policy updated at"
                value={formatTime(data.merge_policy.updated_at)}
              />
              <ProvenanceValue
                code
                label="Merge policy digest"
                value={data.merge_policy.policy_sha256}
              />
            </>
          ) : null}
          {data.repositories.map((repository) => (
            <Fragment key={`provenance:${repository.record_id}`}>
              <ProvenanceValue
                label={`${repository.repository} inventory record ID`}
                value={repository.record_id}
              />
              <ProvenanceValue
                label={`${repository.repository} repository ID`}
                value={repository.repository_id}
              />
              <ProvenanceValue
                label={`${repository.repository} inventory revision`}
                value={String(repository.inventory_revision)}
              />
              <ProvenanceValue
                code
                label={`${repository.repository} inventory digest`}
                value={repository.inventory_sha256}
              />
              <ProvenanceValue
                label={`${repository.repository} inventory recorded at`}
                value={formatTime(repository.recorded_at)}
              />
            </Fragment>
          ))}
          {data.diagnostics.map((diagnostic) => (
            <ProvenanceValue
              key={`diagnostic:${diagnostic.code}:${diagnostic.message}`}
              label="Diagnostic code"
              value={diagnostic.code}
            />
          ))}
        </dl>
      </details>
    </div>
  );
}

function InspectionSetupResult({
  inspectionSetup,
}: {
  inspectionSetup: OrdinaryAgentDeliveryInspectionSetup | undefined;
}) {
  if (!inspectionSetup) {
    return (
      <section
        className="ordinary-agent-inspection-setup"
        aria-label="Inspection setup metadata"
      >
        <header>
          <div>
            <span className="engineering-kicker">Inspection setup</span>
            <strong>Inspection metadata not reported</strong>
          </div>
        </header>
        <p>
          Inspection metadata was not included in this response. App identity
          and installation are not verified by this read. Existing setup
          prerequisites remain the available evidence.
        </p>
      </section>
    );
  }
  return (
    <section
      className="ordinary-agent-inspection-setup"
      aria-label="Inspection setup metadata"
    >
      <header>
        <div>
          <span className="engineering-kicker">Inspection setup</span>
          <strong>{inspectionSetupStateLabel(inspectionSetup.state)}</strong>
        </div>
        <span
          className="ordinary-agent-inspection-setup-state"
          data-state={inspectionSetup.state}
        >
          {inspectionSetup.state === "metadata_recorded"
            ? "Metadata recorded"
            : inspectionSetup.state === "not_evaluated"
              ? "Not evaluated"
              : inspectionSetup.state === "unavailable"
                ? "Unavailable"
                : "Incomplete"}
        </span>
      </header>
      <div className="ordinary-agent-inspection-setup-components">
        <InspectionSetupComponent
          detail={inspectionRuntimeDetail(inspectionSetup.runtime)}
          label="Inspection App metadata"
          state={inspectionSetup.runtime.state}
        />
        <InspectionSetupComponent
          detail={inspectionSecretDetail(inspectionSetup.managed_secret)}
          label="Managed-secret binding metadata"
          state={inspectionSetup.managed_secret.state}
        />
      </div>
      <p className="ordinary-agent-inspection-setup-caveat">
        App identity and installation are not verified by this read. Recorded
        metadata is not provider permission, protection, custody, or readiness
        evidence.
      </p>
      <details className="privileged-operation-policy-review">
        <summary>Inspection metadata details</summary>
        <dl className="privileged-operation-details ordinary-agent-inspection-provenance">
          {inspectionSetup.runtime.state === "metadata_recorded" &&
          inspectionSetup.runtime.app_id ? (
            <ProvenanceValue
              label="Recorded App ID"
              value={inspectionSetup.runtime.app_id}
            />
          ) : null}
          {inspectionSetup.runtime.state === "metadata_recorded" &&
          inspectionSetup.runtime.recorded_at ? (
            <ProvenanceValue
              label="App metadata recorded at"
              value={formatTime(inspectionSetup.runtime.recorded_at)}
            />
          ) : null}
          {inspectionSetup.managed_secret.state === "metadata_recorded" &&
          inspectionSetup.managed_secret.secret_id ? (
            <ProvenanceValue
              label="Managed-secret record ID"
              value={inspectionSetup.managed_secret.secret_id}
            />
          ) : null}
          {inspectionSetup.managed_secret.state === "metadata_recorded" &&
          inspectionSetup.managed_secret.binding_id ? (
            <ProvenanceValue
              label="Managed-secret binding ID"
              value={inspectionSetup.managed_secret.binding_id}
            />
          ) : null}
          {inspectionSetup.managed_secret.state === "metadata_recorded" &&
          inspectionSetup.managed_secret.current_version_id ? (
            <ProvenanceValue
              label="Current version pointer (unverified)"
              value={inspectionSetup.managed_secret.current_version_id}
            />
          ) : null}
        </dl>
      </details>
    </section>
  );
}

function InspectionSetupComponent({
  detail,
  label,
  state,
}: {
  detail: string;
  label: string;
  state: string;
}) {
  return (
    <div className="ordinary-agent-inspection-setup-component">
      <div>
        <span>{label}</span>
        <strong data-state={state}>{inspectionComponentStateLabel(state)}</strong>
      </div>
      <p>{detail}</p>
    </div>
  );
}

function inspectionSetupStateLabel(
  state: OrdinaryAgentDeliveryInspectionSetup["state"],
): string {
  if (state === "metadata_recorded") return "Inspection metadata recorded";
  if (state === "not_evaluated") return "Inspection not evaluated";
  if (state === "unavailable") return "Inspection metadata unavailable";
  return "Inspection setup incomplete";
}

function inspectionComponentStateLabel(state: string): string {
  const labels: Record<string, string> = {
    not_evaluated: "Not evaluated",
    metadata_recorded: "Recorded",
    unavailable: "Unavailable",
    record_missing: "Missing record",
    record_unreadable: "Unable to read",
    record_ambiguous: "Multiple records",
    app_id_missing: "Missing App ID",
    app_id_invalid: "Invalid App ID",
    secret_missing: "Missing secret",
    secret_unreadable: "Unable to read",
    secret_ambiguous: "Multiple secrets",
    binding_missing: "Missing binding",
    binding_unreadable: "Unable to read",
    binding_ambiguous: "Multiple bindings",
    binding_mismatch: "Binding mismatch",
    version_pointer_missing: "Missing current version",
  };
  return labels[state] ?? "Not evaluated";
}

function inspectionRuntimeDetail({
  state,
}: {
  state: OrdinaryAgentDeliveryInspectionRuntimeState;
}): string {
  switch (state) {
    case "metadata_recorded":
      return "The configured App ID is recorded for the inspection scope.";
    case "app_id_missing":
      return "The inspection App ID key is missing from the recorded runtime map.";
    case "app_id_invalid":
      return "The inspection App ID has an invalid recorded shape.";
    case "record_missing":
      return "No matching Launchplane service-context runtime record was found.";
    case "record_unreadable":
      return "The matching runtime record could not be read.";
    case "record_ambiguous":
      return "Multiple matching runtime records prevent selecting one exact runtime record.";
    case "unavailable":
      return "Runtime metadata could not be read.";
    default:
      return "Inspection runtime metadata was not evaluated.";
  }
}

function inspectionSecretDetail({
  state,
}: {
  state: OrdinaryAgentDeliveryInspectionSecretState;
}): string {
  switch (state) {
    case "metadata_recorded":
      return "The exact inspection binding and current-version pointer are recorded.";
    case "secret_missing":
      return "The bounded inspection managed-secret record is missing.";
    case "secret_unreadable":
      return "The inspection managed-secret record could not be read.";
    case "secret_ambiguous":
      return "Multiple matching managed-secret records prevent selecting one exact managed-secret record.";
    case "binding_missing":
      return "The exact inspection binding is missing.";
    case "binding_unreadable":
      return "The exact inspection binding could not be read.";
    case "binding_ambiguous":
      return "Multiple matching inspection bindings prevent selecting one exact binding.";
    case "binding_mismatch":
      return "The binding points to a different managed-secret record.";
    case "version_pointer_missing":
      return "The managed-secret record has no current-version pointer.";
    case "unavailable":
      return "Managed-secret metadata could not be read.";
    default:
      return "Managed-secret inspection metadata was not evaluated.";
  }
}

function ProvenanceValue({
  label,
  value,
  code = false,
}: {
  label: string;
  value: string;
  code?: boolean;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {code ? (
          <code className="privileged-operation-digest">{value}</code>
        ) : (
          value
        )}
      </dd>
    </div>
  );
}

function PreparationFailure({
  icon: Icon,
  message,
  title,
  traceId = "",
}: {
  icon: LucideIcon;
  message: string;
  title: string;
  traceId?: string;
}) {
  return (
    <div className="ordinary-agent-preparation-state" role="alert">
      <Icon size={19} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
        {traceId ? <code>{traceId}</code> : null}
      </div>
    </div>
  );
}

function displayState(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1).replaceAll("_", " ");
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : Boolean(
        error &&
          typeof error === "object" &&
          "name" in error &&
          error.name === "AbortError",
      );
}

async function preparationInputsFixture(
  fixtureMode: Exclude<DevFixtureMode, "">,
  signal: AbortSignal,
): Promise<OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse> {
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(resolve, 60);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
  const requestedState = new URLSearchParams(window.location.search).get("preparation");
  const mode = requestedState || fixtureMode;
  if (mode === "denied") {
    throw new LaunchplaneApiError(
      "This browser session cannot read setup prerequisites.",
      403,
      "fixture-preparation-inputs-denied",
      "authorization_denied",
    );
  }
  if (mode === "error" || mode === "unavailable") {
    throw new LaunchplaneApiError(
      "Setup-prerequisite evidence is unavailable.",
      503,
      "fixture-preparation-inputs-unavailable",
      "service_unavailable",
    );
  }
  const inventoryState: OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse["inventory_state"] =
    mode === "truncated" || mode === "ambiguous" ? mode : "complete";
  const mergePolicyState: OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse["merge_policy_state"] =
    mode === "missing"
      ? "missing"
      : mode === "truncated" || mode === "ambiguous"
        ? mode
        : "available";
  const repositories = mode === "empty" || mode === "truncated"
    ? []
    : [
        {
          record_id: "repository-inventory-1001-r3",
          repository_id: "1001",
          repository: "example/launchplane",
          inventory_revision: 3,
          inventory_sha256: "3".repeat(64),
          recorded_at: "2026-09-12T14:30:00Z",
          configured_branches:
            mergePolicyState === "available" ? ["main", "release"] : [],
        },
        ...(mode === "products"
          ? [{
              record_id: "repository-inventory-1002-r1",
              repository_id: "1002",
              repository: "example/without-branches",
              inventory_revision: 1,
              inventory_sha256: "4".repeat(64),
              recorded_at: "2026-09-12T14:29:00Z",
              configured_branches: [],
            }]
          : []),
      ];
  return {
    status: "ok",
    schema_version: 1,
    trace_id: `fixture-preparation-inputs-${mode}`,
    observed_at: "2026-09-12T14:32:00Z",
    authorization_policy: {
      record_id: "authorization-policy-r7",
      revision: 7,
      schema_version: 2,
      policy_sha256: "1".repeat(64),
    },
    inventory_state: inventoryState,
    merge_policy_state: mergePolicyState,
    merge_policy: mergePolicyState === "available"
      ? {
          record_id: "merge-train-policy-r4",
          policy_sha256: "2".repeat(64),
          updated_at: "2026-09-12T14:25:00Z",
        }
      : null,
    repositories,
    inspection_setup: inspectionSetupFixture(mode),
    terminal_enrollment: { state: "ready" },
    diagnostics: mode === "truncated" || mode === "ambiguous" || mode === "missing"
      ? [{
          code: `${mode}_setup_input`,
          message: `The ${mode} setup input must be resolved before setup planning can use it.`,
        }]
      : [],
  };
}

function inspectionSetupFixture(
  mode: string,
): OrdinaryAgentDeliveryInspectionSetup {
  if (mode === "products") {
    return {
      state: "metadata_recorded",
      runtime: {
        state: "metadata_recorded",
        app_id: "987654321012345",
        recorded_at: "2026-09-12T14:31:00Z",
      },
      managed_secret: {
        state: "metadata_recorded",
        secret_id: "fixture-inspection-secret",
        binding_id: "fixture-inspection-binding",
        current_version_id: "fixture-secret-version-7",
      },
    };
  }
  if (mode === "missing") {
    return {
      state: "incomplete",
      runtime: { state: "app_id_missing", app_id: null, recorded_at: null },
      managed_secret: {
        state: "binding_missing",
        secret_id: null,
        binding_id: null,
        current_version_id: null,
      },
    };
  }
  if (mode === "truncated" || mode === "ambiguous") {
    return {
      state: "incomplete",
      runtime: { state: "record_ambiguous", app_id: null, recorded_at: null },
      managed_secret: {
        state: "binding_ambiguous",
        secret_id: null,
        binding_id: null,
        current_version_id: null,
      },
    };
  }
  return {
    state: "not_evaluated",
    runtime: { state: "not_evaluated", app_id: null, recorded_at: null },
    managed_secret: {
      state: "not_evaluated",
      secret_id: null,
      binding_id: null,
      current_version_id: null,
    },
  };
}
