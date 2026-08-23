import { KeyRound, ShieldAlert } from "lucide-react";
import { useCallback, useState } from "react";

import {
  LaunchplaneApiError,
  approvePrivilegedOperation,
  readPrivilegedOperationPlans,
  revokePrivilegedOperation,
  type PrivilegedOperationListResponse,
  type PrivilegedOperationRecord,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
} from "./engineering-resource";
import {
  EngineeringBoundaryNote,
  EngineeringEmpty,
  EngineeringResourceControls,
  EngineeringResourceGate,
  EngineeringRouteFrame,
} from "./EngineeringRouteUi";
import { formatTime } from "./format";

export function EngineeringPrivilegedOperationsRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<PrivilegedOperationListResponse> => {
      if (fixtureMode) {
        await fixtureDelay(signal);
        return privilegedOperationFixture(fixtureMode);
      }
      return readPrivilegedOperationPlans(signal);
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `privileged-operations:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={resource.cancel}
          refresh={resource.refresh}
          refreshLabel="Refresh plans"
          state={resource.state}
        />
      }
      description="Review typed, redacted privileged-operation plans and record human approvals or revocations without exposing credentials."
      icon={KeyRound}
      title="Privileged operation plans"
      view="privileged-operations"
    >
      <EngineeringBoundaryNote title="Human-governed approval — internal execution only">
        GitHub humans may approve or revoke a current plan. Execution remains a
        service-internal worker action with fresh policy and plan revalidation;
        this UI has no execute control. Agents receive counts only, and the new
        actions ship with no production grants. Managed-secret identifiers and
        version identifiers are never persisted here.
      </EngineeringBoundaryNote>

      <EngineeringResourceGate
        noun="Privileged-operation plans"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <PrivilegedOperationPlanList data={data} refresh={resource.refresh} />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function PrivilegedOperationPlanList({
  data,
  refresh,
}: {
  data: PrivilegedOperationListResponse;
  refresh: () => void;
}) {
  if (!data.records.length) {
    return (
      <EngineeringEmpty
        detail="No typed privileged-operation plan has been recorded. Planning routes remain unavailable until an explicit managed rule is activated through the separate authorization process."
        icon={KeyRound}
        title="No privileged-operation plans"
      />
    );
  }
  return (
    <div
      className="privileged-operation-list"
      aria-label="Privileged-operation plans"
    >
      {data.records.map((record) => (
        <PrivilegedOperationPlanCard
          key={record.operation_id}
          record={record}
          refresh={refresh}
        />
      ))}
    </div>
  );
}

function PrivilegedOperationPlanCard({
  record,
  refresh,
}: {
  record: PrivilegedOperationRecord;
  refresh: () => void;
}) {
  const evidence = record.evidence;
  const [mutationMessage, setMutationMessage] = useState("");
  const canApprove = record.status === "planned";
  const canRevoke = record.status === "approved";

  async function mutate(action: "approve" | "revoke") {
    const reason = window
      .prompt(action === "approve" ? "Approval reason" : "Revocation reason")
      ?.trim();
    if (!reason) return;
    setMutationMessage("");
    try {
      if (action === "approve") {
        await approvePrivilegedOperation(record.operation_id, reason);
      } else {
        await revokePrivilegedOperation(record.operation_id, reason);
      }
      setMutationMessage(
        action === "approve"
          ? "Approval recorded. The service worker will revalidate before execution."
          : "Approval revoked.",
      );
      refresh();
    } catch (error) {
      setMutationMessage(
        error instanceof LaunchplaneApiError
          ? error.message
          : "The operation could not be updated.",
      );
    }
  }
  return (
    <article className="privileged-operation-card">
      <header>
        <div>
          <span className="engineering-kicker">
            Managed-secret re-encryption
          </span>
          <h2>{record.request.reason}</h2>
          <p>
            Requested by <strong>{record.requested_by.login}</strong> · created{" "}
            {formatTime(record.created_at)}
          </p>
        </div>
        <span className={`privileged-operation-status ${record.status}`}>
          {record.status}
        </span>
      </header>

      {evidence.unreadable_secret_count ? (
        <div className="privileged-operation-warning" role="status">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>
            {evidence.unreadable_secret_count} configured secret
            {evidence.unreadable_secret_count === 1 ? " is" : "s are"}{" "}
            unreadable. Raw errors and identifiers are intentionally excluded.
          </span>
        </div>
      ) : null}

      <dl className="privileged-operation-metrics">
        <div>
          <dt>Configured</dt>
          <dd>{evidence.configured_secret_count}</dd>
        </div>
        <div>
          <dt>Would rotate</dt>
          <dd>{evidence.rotation_candidate_count}</dd>
        </div>
        <div>
          <dt>Unchanged</dt>
          <dd>{evidence.unchanged_count}</dd>
        </div>
        <div>
          <dt>Unreadable</dt>
          <dd>{evidence.unreadable_secret_count}</dd>
        </div>
      </dl>

      <dl className="privileged-operation-details">
        <div>
          <dt>Active key</dt>
          <dd>
            <code>{evidence.active_key_id}</code>
          </dd>
        </div>
        <div>
          <dt>Legacy compatibility</dt>
          <dd>
            {evidence.legacy_compatibility_key_loaded ? "Loaded" : "Not loaded"}
          </dd>
        </div>
        <div>
          <dt>Retirement blocked</dt>
          <dd>{keyList(evidence.retirement_blocked_key_ids)}</dd>
        </div>
        <div>
          <dt>Retirement ready</dt>
          <dd>{keyList(evidence.retirement_ready_key_ids)}</dd>
        </div>
        <div>
          <dt>Plan expires</dt>
          <dd>{formatTime(record.expires_at)}</dd>
        </div>
        <div>
          <dt>Plan digest</dt>
          <dd>
            <code className="privileged-operation-digest">
              {evidence.plan_digest}
            </code>
          </dd>
        </div>
      </dl>

      {canApprove || canRevoke ? (
        <div className="privileged-operation-actions">
          {canApprove ? (
            <button type="button" onClick={() => void mutate("approve")}>
              Approve plan
            </button>
          ) : null}
          {canRevoke ? (
            <button type="button" onClick={() => void mutate("revoke")}>
              Revoke approval
            </button>
          ) : null}
        </div>
      ) : null}
      {mutationMessage ? (
        <p className="privileged-operation-terminal-reason">
          {mutationMessage}
        </p>
      ) : null}

      {record.terminal_reason ? (
        <p className="privileged-operation-terminal-reason">
          {record.terminal_reason}
        </p>
      ) : null}
    </article>
  );
}

function keyList(values: string[]): string {
  return values.length ? values.join(", ") : "None";
}

async function fixtureDelay(signal: AbortSignal): Promise<void> {
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
}

function privilegedOperationFixture(
  fixtureMode: Exclude<DevFixtureMode, "">,
): PrivilegedOperationListResponse {
  if (fixtureMode === "error") {
    throw new LaunchplaneApiError(
      "Privileged-operation evidence is unavailable.",
      503,
    );
  }
  if (fixtureMode === "denied") {
    throw new LaunchplaneApiError(
      "This GitHub human does not have privileged-operation read authority.",
      403,
      "fixture-privileged-operation-denied",
      "authorization_denied",
    );
  }
  if (fixtureMode === "empty" || fixtureMode === "missing") {
    return {
      status: "ok",
      trace_id: `fixture-privileged-operation-${fixtureMode}`,
      total: 0,
      records: [],
    };
  }
  return {
    status: "ok",
    trace_id: "fixture-privileged-operation-products",
    total: 1,
    records: [
      {
        schema_version: 1,
        operation_id: "privileged-operation-0123456789abcdef0123456789abcdef",
        descriptor_id: "managed-secret-reencryption",
        descriptor_version: 1,
        safety_class: "secret_backed",
        status: "planned",
        source_event_id: "fixture-request-1",
        requested_by: {
          identity_type: "github_human",
          github_id: 123,
          login: "operator",
        },
        request: {
          schema_version: 1,
          reason: "Review canonical managed-secret root migration",
          source_label: "privileged-operation-plan",
        },
        request_digest: "b".repeat(64),
        evidence: {
          schema_version: 1,
          result_status: "ok",
          plan_digest: "a".repeat(64),
          configured_secret_count: 18,
          rotation_candidate_count: 18,
          unchanged_count: 0,
          unreadable_secret_count: 0,
          active_key_id: "canonical-root-2026",
          retirement_blocked_key_ids: ["legacy-root"],
          retirement_ready_key_ids: [],
          legacy_compatibility_key_loaded: true,
        },
        evidence_digest: "c".repeat(64),
        created_at: "2026-08-22T20:00:00+00:00",
        updated_at: "2026-08-22T20:00:00+00:00",
        expires_at: "2026-08-22T20:30:00+00:00",
        approval: null,
        execution: null,
        terminal_at: "",
        terminal_reason: "",
      },
    ],
  };
}
