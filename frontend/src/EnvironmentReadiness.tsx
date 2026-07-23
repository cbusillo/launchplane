import {
  CircleHelp,
  Database,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  LaunchplaneApiError,
  readProductOperationalReadiness,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { loadDevFixtures } from "./dev-fixture-loader";
import { formatTime } from "./format";
import { InlineError, MissingEvidenceState, humanize } from "./ProductOps";
import {
  buildOperationalReadinessRequest,
  inspectableReadinessActions,
} from "./readiness-model";
import { emptyResource, type ResourceState } from "./resource";

import type {
  ProductActionAvailability,
  ProductEnvironmentDetail,
  ProductOperationalReadiness,
  ProductOperationalReadinessDimension,
} from "./generated/openapi.ts";

type ReadinessState = ProductOperationalReadiness["state"];

export function EnvironmentReadinessPanel({
  actions,
  detail,
  fixtureMode,
}: {
  actions: ProductActionAvailability[];
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
}) {
  const inspectableActions = useMemo(
    () => inspectableReadinessActions(actions),
    [actions],
  );
  const [selectedAuthzAction, setSelectedAuthzAction] = useState(
    inspectableActions[0]?.authz_action ?? "",
  );
  const [retryToken, setRetryToken] = useState(0);
  const [readinessResource, setReadinessResource] = useState<
    ResourceState<ProductOperationalReadiness>
  >(emptyResource());
  const selectionSignature = inspectableActions
    .map((action) => action.authz_action)
    .join("\n");
  const selectedAction =
    inspectableActions.find(
      (action) => action.authz_action === selectedAuthzAction,
    ) ?? inspectableActions[0] ?? null;
  const readinessMatchesSelection =
    readinessResource.data?.action.requested_action ===
    selectedAction?.authz_action;
  const readinessLoading =
    readinessResource.status === "loading" ||
    (readinessResource.status === "ready" && !readinessMatchesSelection);

  useEffect(() => {
    if (!inspectableActions.length) {
      setSelectedAuthzAction("");
      setReadinessResource(emptyResource());
      return;
    }
    if (
      !inspectableActions.some(
        (action) => action.authz_action === selectedAuthzAction,
      )
    ) {
      setSelectedAuthzAction(inspectableActions[0].authz_action);
    }
  }, [inspectableActions, selectedAuthzAction, selectionSignature]);

  useEffect(() => {
    if (!selectedAction) {
      return;
    }
    let active = true;
    const controller = new AbortController();
    setReadinessResource(loadingReadinessResource());

    async function loadReadiness() {
      try {
        let readiness: ProductOperationalReadiness;
        if (fixtureMode) {
          const fixtures = await loadDevFixtures();
          await fixtures.waitForReadinessFixture(controller.signal);
          readiness = fixtures.operationalReadinessForFixture(
            fixtureMode,
            detail,
            selectedAction,
          );
        } else {
          const response = await readProductOperationalReadiness(
            buildOperationalReadinessRequest(detail, selectedAction),
            controller.signal,
          );
          readiness = response.readiness;
        }
        if (!active || controller.signal.aborted) {
          return;
        }
        setReadinessResource({
          status: "ready",
          data: readiness,
          error: "",
          traceId: "",
          statusCode: 200,
        });
      } catch (error) {
        if (!active || controller.signal.aborted) {
          return;
        }
        const apiError = error instanceof LaunchplaneApiError ? error : null;
        const fixtureError = error as {
          message?: string;
          statusCode?: number;
          traceId?: string;
        };
        setReadinessResource({
          status: "error",
          data: null,
          error:
            error instanceof Error
              ? error.message
              : "Operational readiness evidence is unavailable.",
          traceId: apiError?.traceId ?? fixtureError.traceId ?? "",
          statusCode: apiError?.statusCode ?? fixtureError.statusCode ?? 0,
        });
      }
    }

    void loadReadiness();
    return () => {
      active = false;
      controller.abort();
    };
  }, [detail, fixtureMode, retryToken, selectedAction]);

  if (!inspectableActions.length) {
    return (
      <section
        aria-labelledby="operational-readiness-title"
        className="environment-readiness-panel"
      >
        <ReadinessHeader />
        <MissingEvidenceState
          detail="No action exposes one unambiguous primary authorization action, so Launchplane cannot issue an exact readiness read from this browser."
          title="No exact action readiness contract"
        />
      </section>
    );
  }

  return (
    <section
      aria-labelledby="operational-readiness-title"
      className="environment-readiness-panel"
    >
      <ReadinessHeader />
      <div className="readiness-selector">
        <label htmlFor="readiness-action-select">
          <span>Inspect exact action</span>
          <select
            id="readiness-action-select"
            value={selectedAction?.authz_action ?? ""}
            onChange={(event) => {
              setReadinessResource(loadingReadinessResource());
              setSelectedAuthzAction(event.target.value);
            }}
          >
            {inspectableActions.map((action) => (
              <option key={action.authz_action} value={action.authz_action}>
                {action.label} · {humanize(action.safety || "unknown")}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button"
          disabled={readinessLoading}
          type="button"
          onClick={() => {
            setReadinessResource(loadingReadinessResource());
            setRetryToken((current) => current + 1);
          }}
        >
          <RefreshCw
            aria-hidden="true"
            className={readinessLoading ? "spin" : ""}
            size={15}
          />
          Refresh readiness
        </button>
      </div>

      {readinessLoading ? (
        <div
          aria-busy="true"
          aria-live="polite"
          className="readiness-loading"
          role="status"
        >
          <LoaderCircle aria-hidden="true" className="spin" size={20} />
          <div>
            <strong>Evaluating exact readiness</strong>
            <p>
              Launchplane is reading the selected action, lane, artifact, policy,
              and owning evidence records.
            </p>
          </div>
        </div>
      ) : null}

      {readinessResource.status === "error" ? (
        <div className="readiness-error-state">
          <InlineError
            message={readinessResource.error}
            traceId={readinessResource.traceId}
          />
          <p>
            No prior action result is shown because readiness evidence is scoped to
            the exact caller, action, and lane.
          </p>
        </div>
      ) : null}

      {readinessResource.status === "ready" &&
      readinessResource.data &&
      readinessMatchesSelection ? (
        <ReadinessResult readiness={readinessResource.data} />
      ) : null}
    </section>
  );
}

function loadingReadinessResource(): ResourceState<ProductOperationalReadiness> {
  return {
    status: "loading",
    data: null,
    error: "",
    traceId: "",
    statusCode: 0,
  };
}

function ReadinessHeader() {
  return (
    <header className="readiness-panel-header">
      <span aria-hidden="true">
        <ShieldCheck />
      </span>
      <div>
        <p className="eyebrow">Action readiness</p>
        <h2 id="operational-readiness-title">Exact lane and action evidence</h2>
        <p>
          This read is bounded to Launchplane-owned context, instance, artifact,
          policy, and evidence records. Returned remediation routes remain evidence
          unless a typed browser control or reviewed workflow owns execution.
        </p>
      </div>
    </header>
  );
}

function ReadinessResult({
  readiness,
}: {
  readiness: ProductOperationalReadiness;
}) {
  const readyDimensions = readiness.dimensions.filter(
    (dimension) => dimension.state === "ready",
  ).length;
  const requiredDimensions = readiness.dimensions.filter(
    (dimension) => dimension.required,
  ).length;
  const workflowCaller = readiness.caller.identity_type === "github_actions";
  return (
    <div className="readiness-result" data-state={readiness.state}>
      <header className="readiness-result-summary">
        <span className="readiness-result-icon" aria-hidden="true">
          <ReadinessStateIcon state={readiness.state} />
        </span>
        <div>
          <p className="eyebrow">{readiness.action.label || "Selected action"}</p>
          <h3>{readinessSummary(readiness)}</h3>
          <p>
            Generated {formatTime(readiness.generated_at)} for context{
              " "
            }
            <code>{readiness.context}</code> and instance <code>{readiness.instance}</code>.
          </p>
        </div>
        <ReadinessBadge state={readiness.state} />
      </header>

      <div className="readiness-summary-strip" aria-label="Readiness summary">
        <div>
          <strong>{readyDimensions}</strong>
          <span>dimensions ready</span>
        </div>
        <div>
          <strong>{requiredDimensions}</strong>
          <span>required dimensions</span>
        </div>
        <div>
          <strong>{readiness.non_ready_dimensions.length}</strong>
          <span>need attention</span>
        </div>
        <div>
          <strong>{readiness.action.supported ? "Yes" : "No"}</strong>
          <span>action supported</span>
        </div>
      </div>

      <section
        aria-label={
          workflowCaller
            ? "GitHub Actions workflow identity"
            : "Browser identity limitation"
        }
        className="readiness-caller-note"
        data-workflow-caller={workflowCaller}
      >
        <GitBranch aria-hidden="true" size={18} />
        <div>
          <strong>
            {workflowCaller
              ? "Immutable workflow identity evaluated"
              : "Browser evidence view — not workflow authorization"}
          </strong>
          <p>
            {workflowCaller
              ? "Launchplane evaluated the pinned caller and reusable workflow identity against the active managed policy."
              : "This response evaluates the signed-in browser identity. A reviewed GitHub Actions worker must run the same preflight to prove immutable workflow authorization."}
          </p>
          {workflowCaller ? (
            <div className="readiness-workflow-identity">
              <code>{readiness.caller.workflow_ref}</code>
              <code>{readiness.caller.job_workflow_ref}</code>
            </div>
          ) : null}
        </div>
      </section>

      <dl className="readiness-action-contract">
        <div>
          <dt>Requested action</dt>
          <dd><code>{readiness.action.requested_action}</code></dd>
        </div>
        <div>
          <dt>Safety</dt>
          <dd>{humanize(readiness.action.safety || "unknown")}</dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd>{humanize(readiness.action.scope || "unknown")}</dd>
        </div>
        <div>
          <dt>Service contract</dt>
          <dd><code>{readiness.action.method} {readiness.action.route_path}</code></dd>
        </div>
      </dl>

      {readiness.dimensions.length ? (
        <ul className="readiness-dimension-list" aria-label="Readiness dimensions">
          {readiness.dimensions.map((dimension) => (
            <ReadinessDimensionCard
              dimension={dimension}
              key={dimension.dimension}
            />
          ))}
        </ul>
      ) : (
        <MissingEvidenceState
          detail="The readiness response contained no bounded dimensions. The selected action remains non-executable in this browser."
          title="No readiness dimensions returned"
        />
      )}
    </div>
  );
}

function ReadinessDimensionCard({
  dimension,
}: {
  dimension: ProductOperationalReadinessDimension;
}) {
  const advisory = dimension.state === "ready" && dimension.details.length > 0;
  return (
    <li className="readiness-dimension-card" data-state={dimension.state}>
      <header>
        <span aria-hidden="true">
          <ReadinessStateIcon state={dimension.state} />
        </span>
        <div>
          <p>{dimension.required ? "Required dimension" : "Optional dimension"}</p>
          <h4>{humanize(dimension.dimension)}</h4>
        </div>
        <ReadinessBadge state={dimension.state} />
      </header>
      <p className="readiness-dimension-summary">{dimension.summary}</p>
      <div className="readiness-owner">
        <Database aria-hidden="true" size={14} />
        <span>Owned by</span>
        <code>{dimension.owner_record_type}</code>
      </div>

      {dimension.details.length ? (
        <section className="readiness-details" data-advisory={advisory}>
          {advisory ? (
            <CircleHelp aria-hidden="true" size={15} />
          ) : (
            <TriangleAlert aria-hidden="true" size={15} />
          )}
          <div>
            <strong>{advisory ? "Advisory — does not block" : "Blocking details"}</strong>
            <ul>
              {dimension.details.map((detail) => (
                <li key={detail}><code>{detail}</code></li>
              ))}
            </ul>
          </div>
        </section>
      ) : null}

      <section className="readiness-evidence">
        <strong>Bounded evidence</strong>
        {dimension.evidence.length ? (
          <ul>
            {dimension.evidence.map((evidence, index) => (
              <li key={`${evidence.record_type}:${evidence.record_id}:${index}`}>
                <div>
                  <code>{evidence.record_type}</code>
                  <span>{evidence.record_id || "Record identifier unavailable"}</span>
                </div>
                <div>
                  <span>{humanize(evidence.freshness_status)}</span>
                  {evidence.revision ? <span>Revision {evidence.revision}</span> : null}
                  {evidence.recorded_at ? <time>{formatTime(evidence.recorded_at)}</time> : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p>No bounded record identifier was returned for this dimension.</p>
        )}
      </section>

      {dimension.remediation ? (
        <section className="readiness-remediation">
          <ShieldAlert aria-hidden="true" size={16} />
          <div>
            <strong>Supported remediation metadata</strong>
            <p>{dimension.remediation.summary}</p>
            <code>
              {dimension.remediation.method || "SERVICE"}{" "}
              {dimension.remediation.route_path}
            </code>
            <small>
              Evidence only. This browser does not invoke descriptor or remediation
              routes dynamically.
            </small>
          </div>
        </section>
      ) : dimension.state !== "ready" ? (
        <section className="readiness-no-remediation">
          <CircleHelp aria-hidden="true" size={15} />
          <p>No typed no-effect remediation is available in this browser.</p>
        </section>
      ) : null}
    </li>
  );
}

function ReadinessBadge({ state }: { state: ReadinessState }) {
  return (
    <span className="readiness-badge" data-state={state}>
      <span aria-hidden="true" />
      {humanize(state)}
    </span>
  );
}

function ReadinessStateIcon({ state }: { state: ReadinessState }) {
  if (state === "ready") {
    return <ShieldCheck />;
  }
  if (state === "blocked") {
    return <ShieldAlert />;
  }
  if (state === "stale") {
    return <TriangleAlert />;
  }
  return <CircleHelp />;
}

function readinessSummary(readiness: ProductOperationalReadiness): string {
  if (readiness.state === "ready") {
    return "All required readiness dimensions pass";
  }
  if (!readiness.non_ready_dimensions.length) {
    return `${humanize(readiness.state)} readiness evidence`;
  }
  return `${readiness.non_ready_dimensions.length} dimension${
    readiness.non_ready_dimensions.length === 1 ? "" : "s"
  } ${readiness.non_ready_dimensions.length === 1 ? "needs" : "need"} attention`;
}
