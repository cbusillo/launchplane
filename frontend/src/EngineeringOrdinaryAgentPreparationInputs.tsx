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
  type OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { formatTime } from "./format";

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
  const incompleteInventory = data.inventory_state !== "complete";
  const mergePolicyAvailable = data.merge_policy_state === "available";

  return (
    <div className="ordinary-agent-preparation-result">
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
                <span>Configured</span>
              </header>
              <div>
                <span>Configured branches</span>
                {repository.configured_branches.length ? (
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
  const repositories = mode === "empty"
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
            mode === "missing" ? [] : ["main", "release"],
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
    diagnostics: mode === "truncated" || mode === "ambiguous" || mode === "missing"
      ? [{
          code: `${mode}_setup_input`,
          message: `The ${mode} setup input must be resolved before setup planning can use it.`,
        }]
      : [],
  };
}
