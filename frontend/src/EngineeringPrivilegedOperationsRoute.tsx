import { KeyRound, ShieldAlert } from "lucide-react";
import { useCallback, useState } from "react";

import {
  LaunchplaneApiError,
  approvePrivilegedOperation,
  readPrivilegedOperationPlans,
  revokePrivilegedOperation,
  type PrivilegedOperationDescriptorId,
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
  const [descriptorId, setDescriptorId] =
    useState<PrivilegedOperationDescriptorId>("managed-secret-reencryption");
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<PrivilegedOperationListResponse> => {
      if (fixtureMode) {
        await fixtureDelay(signal);
        return privilegedOperationFixture(fixtureMode, descriptorId);
      }
      return readPrivilegedOperationPlans(signal, descriptorId);
    },
    [descriptorId, fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `privileged-operations:${descriptorId}:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <div className="privileged-operation-toolbar">
          <div
            className="privileged-operation-kind-switch"
            aria-label="Operation type"
          >
            <button
              aria-pressed={descriptorId === "managed-secret-reencryption"}
              onClick={() => setDescriptorId("managed-secret-reencryption")}
              type="button"
            >
              Secret rotation
            </button>
            <button
              aria-pressed={descriptorId === "managed-authz-policy-set"}
              onClick={() => setDescriptorId("managed-authz-policy-set")}
              type="button"
            >
              Policy changes
            </button>
          </div>
          <EngineeringResourceControls
            cancel={resource.cancel}
            refresh={resource.refresh}
            refreshLabel="Refresh plans"
            state={resource.state}
          />
        </div>
      }
      description="Review typed, redacted privileged-operation plans and record human approvals or revocations without exposing credentials."
      icon={KeyRound}
      title="Privileged operation plans"
      view="privileged-operations"
    >
      <EngineeringBoundaryNote title="Human-governed approval — internal execution only">
        GitHub humans may approve or revoke a current plan. Execution remains a
        service-internal worker action with fresh policy and plan revalidation;
        this UI has no execute control. Agents may submit inert policy proposals
        and receive only their own bounded summaries. The new actions ship with
        no production grants.
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
  const policyEvidence = "diff" in evidence ? evidence : null;
  const secretEvidence =
    "configured_secret_count" in evidence ? evidence : null;
  const policyRequest =
    "desired_policy" in record.request ? record.request : null;
  const [mutationMessage, setMutationMessage] = useState("");
  const canApprove =
    record.status === "planned" &&
    (!policyEvidence || policyEvidence.result_status === "ok");
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
            {policyEvidence
              ? "Managed authorization policy"
              : "Managed-secret re-encryption"}
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

      {secretEvidence?.unreadable_secret_count ? (
        <div className="privileged-operation-warning" role="status">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>
            {secretEvidence.unreadable_secret_count} configured secret
            {secretEvidence.unreadable_secret_count === 1
              ? " is"
              : "s are"}{" "}
            unreadable. Raw errors and identifiers are intentionally excluded.
          </span>
        </div>
      ) : null}

      {policyEvidence?.result_status === "blocked" ? (
        <div className="privileged-operation-warning" role="status">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>
            This policy proposal has safety or operational-readiness blockers
            and cannot be approved.
          </span>
        </div>
      ) : null}

      <dl className="privileged-operation-metrics">
        {secretEvidence ? (
          <>
            <div>
              <dt>Configured</dt>
              <dd>{secretEvidence.configured_secret_count}</dd>
            </div>
            <div>
              <dt>Would rotate</dt>
              <dd>{secretEvidence.rotation_candidate_count}</dd>
            </div>
            <div>
              <dt>Unchanged</dt>
              <dd>{secretEvidence.unchanged_count}</dd>
            </div>
            <div>
              <dt>Unreadable</dt>
              <dd>{secretEvidence.unreadable_secret_count}</dd>
            </div>
          </>
        ) : null}
        {policyEvidence ? (
          <>
            <div>
              <dt>Added</dt>
              <dd>{policyEvidence.diff.added_rule_count}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{policyEvidence.diff.updated_rule_count}</dd>
            </div>
            <div>
              <dt>Removed</dt>
              <dd>{policyEvidence.diff.removed_rule_count}</dd>
            </div>
            <div>
              <dt>Blockers</dt>
              <dd>
                {policyEvidence.diff.policy_safety_blocker_count +
                  policyEvidence.diff.operational_readiness_blocked_rule_count}
              </dd>
            </div>
          </>
        ) : null}
      </dl>

      <dl className="privileged-operation-details">
        {secretEvidence ? (
          <>
            <div>
              <dt>Active key</dt>
              <dd>
                <code>{secretEvidence.active_key_id}</code>
              </dd>
            </div>
            <div>
              <dt>Legacy compatibility</dt>
              <dd>
                {secretEvidence.legacy_compatibility_key_loaded
                  ? "Loaded"
                  : "Not loaded"}
              </dd>
            </div>
            <div>
              <dt>Retirement blocked</dt>
              <dd>{keyList(secretEvidence.retirement_blocked_key_ids)}</dd>
            </div>
            <div>
              <dt>Retirement ready</dt>
              <dd>{keyList(secretEvidence.retirement_ready_key_ids)}</dd>
            </div>
          </>
        ) : null}
        {policyEvidence && policyRequest ? (
          <>
            <div>
              <dt>Managed set</dt>
              <dd>
                <code>{policyRequest.managed_set_id}</code>
              </dd>
            </div>
            <div>
              <dt>Policy revision</dt>
              <dd>
                {policyEvidence.diff.previous_revision} →{" "}
                {policyEvidence.diff.candidate_revision}
              </dd>
            </div>
            <div>
              <dt>Desired policy SHA</dt>
              <dd>
                <code className="privileged-operation-digest">
                  {policyEvidence.diff.desired_policy_sha256}
                </code>
              </dd>
            </div>
            <div>
              <dt>Related issue</dt>
              <dd>{policyRequest.related_issue || "Not supplied"}</dd>
            </div>
          </>
        ) : null}
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

      {policyRequest ? (
        <details className="privileged-operation-policy-review">
          <summary>Review exact desired policy</summary>
          <pre>{JSON.stringify(policyRequest.desired_policy, null, 2)}</pre>
        </details>
      ) : null}

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
  descriptorId: PrivilegedOperationDescriptorId,
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
    records:
      descriptorId === "managed-authz-policy-set"
        ? [policyFixtureRecord()]
        : [
            {
              schema_version: 1,
              operation_id:
                "privileged-operation-0123456789abcdef0123456789abcdef",
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

function policyFixtureRecord(): PrivilegedOperationRecord {
  return {
    schema_version: 1,
    operation_id: "privileged-operation-fedcba9876543210fedcba9876543210",
    descriptor_id: "managed-authz-policy-set",
    descriptor_version: 1,
    safety_class: "policy_admin",
    status: "planned",
    source_event_id: "fixture-policy-request-1",
    requested_by: {
      identity_type: "terminal_agent",
      login: "terminal-agent",
      principal_sha256: "d".repeat(64),
    },
    request: {
      schema_version: 1,
      managed_set_id: "authorization.canary",
      reason: "Review a DB-native policy canary proposal",
      related_issue: "cbusillo/launchplane#2238",
      desired_policy: {
        schema_version: 2,
        github_actions: [],
        github_humans: [
          {
            actions: ["authz_policy_operation.read"],
            contexts: ["launchplane"],
            github_ids: [789],
            logins: [],
            managed_rule_id: "policy-canary-reader",
            managed_set_id: "authorization.canary",
            organizations: [],
            products: ["launchplane"],
            roles: ["admin"],
            teams: [],
          },
        ],
        local_admins: [],
        local_operators: [],
        terminal_agents: [],
      },
    },
    request_digest: "e".repeat(64),
    evidence: {
      schema_version: 1,
      result_status: "ok",
      plan_digest: "f".repeat(64),
      diff: {
        managed_set_id: "authorization.canary",
        previous_record_id: "launchplane-authz-policy-r1",
        previous_revision: 1,
        candidate_revision: 2,
        previous_policy_sha256: "1".repeat(64),
        desired_policy_sha256: "2".repeat(64),
        desired_set_sha256: "3".repeat(64),
        plan_sha256: "f".repeat(64),
        schema_migrated: false,
        changed: true,
        authorization_changed: true,
        added_rule_count: 1,
        adopted_rule_count: 0,
        updated_rule_count: 0,
        removed_rule_count: 0,
        unchanged_rule_count: 0,
        unmanaged_compatibility_candidate_count: 0,
        retired_unmanaged_compatibility_rule_count: 0,
        retired_unmanaged_compatibility_rules: [],
        policy_safety_blocker_count: 0,
        policy_safety_blockers: [],
        operational_readiness_blocked_rule_count: 0,
        operational_readiness_blockers: [],
        changes: [],
      },
    },
    evidence_digest: "4".repeat(64),
    created_at: "2026-08-25T20:00:00+00:00",
    updated_at: "2026-08-25T20:00:00+00:00",
    expires_at: "2026-08-25T20:30:00+00:00",
    approval: null,
    execution: null,
    terminal_at: "",
    terminal_reason: "",
  };
}
