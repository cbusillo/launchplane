import {
  Download,
  History,
  KeyRound,
  RotateCcw,
  SearchCheck,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useState, type SyntheticEvent } from "react";

import {
  LaunchplaneApiError,
  buildAuthzManagedSetRollbackProposal,
  evaluateEffectiveAccess,
  explainAuthzDenial,
  exportActiveAuthzPolicy,
  planPrivilegedOperation,
  readAuthzPolicyAdministration,
  readAuthzPolicyRevisionHistory,
  type AuthzActivePolicyExportResponse,
  type AuthzDenialExplanationResponse,
  type AuthzManagedSetRollbackProposalResponse,
  type AuthzPolicyAdministrationReadResponse,
  type AuthzPolicyRevisionHistoryResponse,
  type EffectiveAccessEvaluateRequest,
  type EffectiveAccessEvaluateResponse,
  type PrivilegedOperationPlanEnvelope,
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
import { AppLink, engineeringPath } from "./router";

interface AuthzAdministrationWorkspace {
  administration: AuthzPolicyAdministrationReadResponse;
  history: AuthzPolicyRevisionHistoryResponse;
}

interface ActionNotice {
  tone: "idle" | "working" | "success" | "error";
  message: string;
  traceId: string;
}

const EMPTY_NOTICE: ActionNotice = {
  tone: "idle",
  message: "",
  traceId: "",
};

const EMPTY_POLICY = JSON.stringify(
  {
    schema_version: 2,
    github_actions: [],
    github_humans: [],
    terminal_agents: [],
    local_operators: [],
    local_admins: [],
  },
  null,
  2,
);

const DEFAULT_HUMAN_PRINCIPAL = JSON.stringify(
  {
    principal_type: "github_human",
    login: "operator-login",
    github_id: 1,
    organizations: [],
    teams: [],
    role: "admin",
  },
  null,
  2,
);

export function EngineeringAuthorizationAdministrationRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<AuthzAdministrationWorkspace> => {
      if (fixtureMode) {
        await fixtureDelay(signal);
        return authzAdministrationFixture(fixtureMode);
      }
      const [administration, history] = await Promise.all([
        readAuthzPolicyAdministration(signal),
        readAuthzPolicyRevisionHistory(signal),
      ]);
      return { administration, history };
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `authorization-administration:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={resource.cancel}
          refresh={resource.refresh}
          refreshLabel="Refresh policy"
          state={resource.state}
        />
      }
      description="Inspect the active DB-backed policy, test one exact access question, explain a denial, and prepare reviewed policy proposals without making GitHub the desired-state store."
      icon={ShieldCheck}
      title="Authorization administration"
      view="authorization-administration"
    >
      <EngineeringBoundaryNote title="DB-native authority — production apply remains approval-stopped">
        Inspection, export, rollback preparation, and proposal dry-runs do not
        grant access. Approved plans execute only through the service worker
        after fresh policy, digest, revision, idempotency, and administrator-
        continuity checks. Total-lockout and break-glass recovery remain
        explicitly deferred.
      </EngineeringBoundaryNote>

      <EngineeringResourceGate
        noun="authorization administration evidence"
        refresh={resource.refresh}
        state={resource.state}
      >
        {data => (
          <AuthorizationPolicyWorkspace data={data} refresh={resource.refresh} />
        )}
      </EngineeringResourceGate>

      <div className="authz-admin-independent-tools">
        <EffectiveAccessTool fixtureMode={fixtureMode} />
        <DenialExplanationTool fixtureMode={fixtureMode} />
      </div>
    </EngineeringRouteFrame>
  );
}

function AuthorizationPolicyWorkspace({
  data,
  refresh,
}: {
  data: AuthzAdministrationWorkspace;
  refresh: () => void;
}) {
  const { administration, history } = data;
  return (
    <div className="authz-admin-workspace">
      <section className="authz-admin-policy-card" data-health={administration.health.state}>
        <header>
          <div>
            <span className="engineering-kicker">Active DB policy</span>
            <h2>Revision {administration.policy.revision}</h2>
            <p>
              Updated {formatTime(administration.policy.updated_at)} · schema v
              {administration.policy.schema_version}
            </p>
          </div>
          <span className="authz-admin-health">{humanize(administration.health.state)}</span>
        </header>
        <dl className="authz-admin-metrics">
          <Metric label="Managed sets" value={administration.managed_sets.total_count} />
          <Metric label="Managed rules" value={administration.health.managed_rule_count} />
          <Metric label="Unmanaged rules" value={administration.health.unmanaged_rule_count} />
          <Metric
            label="Independent admins"
            value={administration.reachable_administrators.independent_from_caller_rule_count}
          />
        </dl>
        {administration.health.reason_codes.length ? (
          <div className="authz-admin-reasons" role="status">
            {administration.health.reason_codes.map((reason) => (
              <span key={reason}>{humanize(reason)}</span>
            ))}
          </div>
        ) : null}
        <dl className="authz-admin-provenance">
          <div>
            <dt>Record</dt>
            <dd><code>{administration.policy.record_id}</code></dd>
          </div>
          <div>
            <dt>Policy digest</dt>
            <dd><code>{administration.policy.policy_sha256}</code></dd>
          </div>
          <div>
            <dt>Applying administrator retained</dt>
            <dd>{yesNo(administration.reachable_administrators.caller_has_policy_administration)}</dd>
          </div>
          <div>
            <dt>Independent administrator reachable</dt>
            <dd>{yesNo(administration.reachable_administrators.independent_from_caller_reachable)}</dd>
          </div>
        </dl>
      </section>

      <section className="authz-admin-section">
        <header>
          <div>
            <span className="engineering-kicker">Managed authority</span>
            <h2>Rule sets and principals</h2>
          </div>
        </header>
        {administration.managed_sets.items.length ? (
          <div className="authz-admin-managed-set-grid">
            {administration.managed_sets.items.map((managedSet) => (
              <article key={managedSet.managed_set_id}>
                <strong>{managedSet.managed_set_id}</strong>
                <span>{managedSet.rule_count} rule{managedSet.rule_count === 1 ? "" : "s"}</span>
                <small>{principalCountSummary(managedSet.principal_rule_counts)}</small>
              </article>
            ))}
          </div>
        ) : (
          <EngineeringEmpty
            detail="The active policy contains no managed rule sets. This is evidence, not permission to create one."
            icon={KeyRound}
            title="No managed sets"
          />
        )}
        <details className="authz-admin-rule-details">
          <summary>Bounded managed-rule identities</summary>
          <div>
            {administration.managed_rules.items.map((rule) => (
              <p key={`${rule.managed_set_id}:${rule.managed_rule_id}`}>
                <strong>{rule.managed_rule_id}</strong>
                <span>{rule.managed_set_id} · {humanize(rule.principal_type)}</span>
                <code>{rule.rule_sha256}</code>
              </p>
            ))}
          </div>
        </details>
      </section>

      <div className="authz-admin-action-grid">
        <PolicyProposalTool refresh={refresh} />
        <RollbackTool history={history} refresh={refresh} />
      </div>
      <PolicyHistory history={history} />
      <PolicyExportTool policy={administration.policy} />
    </div>
  );
}

function PolicyProposalTool({ refresh }: { refresh: () => void }) {
  const [managedSetId, setManagedSetId] = useState("");
  const [reason, setReason] = useState("");
  const [relatedIssue, setRelatedIssue] = useState("");
  const [policyText, setPolicyText] = useState(EMPTY_POLICY);
  const [notice, setNotice] = useState<ActionNotice>(EMPTY_NOTICE);

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice({ tone: "working", message: "Computing and recording the non-authoritative dry-run…", traceId: "" });
    try {
      const desiredPolicy = JSON.parse(policyText) as Record<string, unknown>;
      const response = await planPrivilegedOperation({
        schema_version: 1,
        descriptor_id: "managed-authz-policy-set",
        source_event_id: `ui:authz-proposal:${Date.now()}`,
        request: {
          schema_version: 1,
          managed_set_id: managedSetId,
          desired_policy: desiredPolicy,
          reason,
          related_issue: relatedIssue,
        },
      } as PrivilegedOperationPlanEnvelope);
      setNotice({
        tone: "success",
        message: `Proposal ${response.record.operation_id} recorded as ${response.record.status}. Review its exact digest before approval.`,
        traceId: response.trace_id,
      });
      refresh();
    } catch (error) {
      setNotice(actionFailure(error, "The authorization proposal could not be recorded."));
    }
  }

  return (
    <section className="authz-admin-tool-card">
      <header>
        <KeyRound size={20} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Propose, dry-run, revoke, restore</span>
          <h2>Managed-set proposal</h2>
        </div>
      </header>
      <p>
        Submit one exact managed set to the existing human-governed planner. An
        empty schema-v2 policy proposes removal of that set; importing an
        exported set proposes restore. No apply occurs here.
      </p>
      <form className="authz-admin-form" onSubmit={submit}>
        <label>
          <span>Managed set ID</span>
          <input required value={managedSetId} onChange={(event) => setManagedSetId(event.target.value)} />
        </label>
        <label>
          <span>Reason</span>
          <input required value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <label>
          <span>Related issue</span>
          <input value={relatedIssue} onChange={(event) => setRelatedIssue(event.target.value)} />
        </label>
        <label className="authz-admin-wide-field">
          <span>Desired schema-v2 policy JSON</span>
          <textarea required rows={12} spellCheck={false} value={policyText} onChange={(event) => setPolicyText(event.target.value)} />
        </label>
        <button className="button primary" disabled={notice.tone === "working"} type="submit">
          Record proposal dry-run
        </button>
      </form>
      <ActionMessage notice={notice} />
      <AppLink className="authz-admin-inline-link" to={engineeringPath("privileged-operations")}>
        Open approval and revocation queue
      </AppLink>
    </section>
  );
}

function RollbackTool({
  history,
  refresh,
}: {
  history: AuthzPolicyRevisionHistoryResponse;
  refresh: () => void;
}) {
  const [targetRevision, setTargetRevision] = useState(
    String(history.revisions[1]?.policy.revision ?? history.revisions[0]?.policy.revision ?? 1),
  );
  const [managedSetId, setManagedSetId] = useState("");
  const [reason, setReason] = useState("");
  const [relatedIssue, setRelatedIssue] = useState("");
  const [prepared, setPrepared] = useState<AuthzManagedSetRollbackProposalResponse | null>(null);
  const [notice, setNotice] = useState<ActionNotice>(EMPTY_NOTICE);

  async function prepare(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice({ tone: "working", message: "Reconstructing the historical managed set…", traceId: "" });
    try {
      const response = await buildAuthzManagedSetRollbackProposal({
        schema_version: 1,
        target_revision: Number(targetRevision),
        managed_set_id: managedSetId,
        reason,
        related_issue: relatedIssue,
        source_event_id: `ui:authz-rollback:${Date.now()}`,
      });
      setPrepared(response);
      setNotice({
        tone: "success",
        message: `Revision ${response.target_policy.revision} was reconstructed as a forward proposal. No policy was changed.`,
        traceId: response.trace_id,
      });
    } catch (error) {
      setPrepared(null);
      setNotice(actionFailure(error, "The rollback proposal could not be prepared."));
    }
  }

  async function recordPrepared() {
    if (!prepared) return;
    setNotice({ tone: "working", message: "Recording the prepared rollback dry-run…", traceId: "" });
    try {
      const response = await planPrivilegedOperation(
        prepared.proposal as PrivilegedOperationPlanEnvelope,
      );
      setNotice({
        tone: "success",
        message: `Rollback proposal ${response.record.operation_id} recorded. It still requires exact review and approval.`,
        traceId: response.trace_id,
      });
      setPrepared(null);
      refresh();
    } catch (error) {
      setNotice(actionFailure(error, "The prepared rollback proposal could not be recorded."));
    }
  }

  return (
    <section className="authz-admin-tool-card">
      <header>
        <RotateCcw size={20} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Forward-only rollback</span>
          <h2>Restore one managed set</h2>
        </div>
      </header>
      <p>
        Historical rows remain immutable. Rollback reconstructs one managed set
        and sends it through the ordinary proposal, approval, CAS, idempotency,
        and worker read-back path as a new revision.
      </p>
      <form className="authz-admin-form" onSubmit={prepare}>
        <label>
          <span>Target revision</span>
          <input min="1" required type="number" value={targetRevision} onChange={(event) => setTargetRevision(event.target.value)} />
        </label>
        <label>
          <span>Managed set ID</span>
          <input required value={managedSetId} onChange={(event) => setManagedSetId(event.target.value)} />
        </label>
        <label>
          <span>Reason</span>
          <input required value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <label>
          <span>Related issue</span>
          <input value={relatedIssue} onChange={(event) => setRelatedIssue(event.target.value)} />
        </label>
        <button className="button" disabled={notice.tone === "working"} type="submit">
          Prepare rollback
        </button>
        {prepared ? (
          <button className="button primary" disabled={notice.tone === "working"} onClick={recordPrepared} type="button">
            Record prepared proposal
          </button>
        ) : null}
      </form>
      <ActionMessage notice={notice} />
    </section>
  );
}

function PolicyHistory({ history }: { history: AuthzPolicyRevisionHistoryResponse }) {
  return (
    <section className="authz-admin-section">
      <header>
        <History size={20} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Append-only audit</span>
          <h2>Policy revisions</h2>
        </div>
      </header>
      <div className="authz-admin-history">
        {history.revisions.map((entry) => (
          <article key={entry.policy.record_id}>
            <div>
              <strong>Revision {entry.policy.revision}</strong>
              <span>{entry.policy.status} · {formatTime(entry.policy.updated_at)}</span>
            </div>
            <code>{entry.policy.policy_sha256}</code>
            <small>
              {entry.audit.audit_present
                ? `${entry.audit.operation || "recorded change"} · ${entry.audit.managed_set_id || "policy"}`
                : "Legacy record without structured audit"}
            </small>
          </article>
        ))}
      </div>
      {history.truncated ? <p className="authz-admin-caution">History is bounded to the newest {history.returned_count} revisions.</p> : null}
    </section>
  );
}

function PolicyExportTool({
  policy,
}: {
  policy: AuthzPolicyAdministrationReadResponse["policy"];
}) {
  const [notice, setNotice] = useState<ActionNotice>(EMPTY_NOTICE);

  async function download() {
    setNotice({ tone: "working", message: "Reading the exact active policy export…", traceId: "" });
    try {
      const response = await exportActiveAuthzPolicy();
      downloadJson(
        `launchplane-authz-policy-r${response.policy.revision}.json`,
        response,
      );
      setNotice({
        tone: "success",
        message: `Exported revision ${response.policy.revision} with policy digest ${response.policy.policy_sha256}.`,
        traceId: response.trace_id,
      });
    } catch (error) {
      setNotice(actionFailure(error, "The active policy export could not be read."));
    }
  }

  return (
    <section className="authz-admin-export">
      <div>
        <Download size={20} aria-hidden="true" />
        <div>
          <strong>Export active policy</strong>
          <p>
            Full-fidelity export contains sensitive selectors and principals.
            It is available only to a human administrator with proposal
            authority and is never GitHub-hosted desired state.
          </p>
          <code>{policy.policy_sha256}</code>
        </div>
      </div>
      <button className="button" disabled={notice.tone === "working"} onClick={download} type="button">
        Download JSON
      </button>
      <ActionMessage notice={notice} />
    </section>
  );
}

function EffectiveAccessTool({ fixtureMode }: { fixtureMode: DevFixtureMode }) {
  const [action, setAction] = useState("authz_policy_administration.read");
  const [product, setProduct] = useState("launchplane");
  const [context, setContext] = useState("launchplane");
  const [targetScope, setTargetScope] = useState<"context" | "instance">("context");
  const [instance, setInstance] = useState("");
  const [principalText, setPrincipalText] = useState(DEFAULT_HUMAN_PRINCIPAL);
  const [result, setResult] = useState<EffectiveAccessEvaluateResponse | null>(null);
  const [notice, setNotice] = useState<ActionNotice>(EMPTY_NOTICE);

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice({ tone: "working", message: "Evaluating one exact access request…", traceId: "" });
    try {
      const request = {
        action,
        product,
        context,
        target_scope: targetScope,
        instance: targetScope === "instance" ? instance : "",
        principal: JSON.parse(principalText),
      } as EffectiveAccessEvaluateRequest;
      const response = fixtureMode
        ? effectiveAccessFixture(request)
        : await evaluateEffectiveAccess(request);
      setResult(response);
      setNotice({ tone: "success", message: `Decision: ${response.evaluation.decision}.`, traceId: response.trace_id });
    } catch (error) {
      setResult(null);
      setNotice(actionFailure(error, "Effective access could not be evaluated."));
    }
  }

  return (
    <section className="authz-admin-tool-card">
      <header>
        <SearchCheck size={20} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Separately grantable read</span>
          <h2>Effective access</h2>
        </div>
      </header>
      <form className="authz-admin-form" onSubmit={submit}>
        <label><span>Action</span><input required value={action} onChange={(event) => setAction(event.target.value)} /></label>
        <label><span>Product</span><input required value={product} onChange={(event) => setProduct(event.target.value)} /></label>
        <label><span>Context</span><input required value={context} onChange={(event) => setContext(event.target.value)} /></label>
        <label>
          <span>Target scope</span>
          <select value={targetScope} onChange={(event) => setTargetScope(event.target.value as "context" | "instance")}>
            <option value="context">Context</option>
            <option value="instance">Instance</option>
          </select>
        </label>
        {targetScope === "instance" ? (
          <label><span>Instance</span><input required value={instance} onChange={(event) => setInstance(event.target.value)} /></label>
        ) : null}
        <label className="authz-admin-wide-field">
          <span>Exact principal JSON</span>
          <textarea rows={9} spellCheck={false} value={principalText} onChange={(event) => setPrincipalText(event.target.value)} />
        </label>
        <button className="button" disabled={notice.tone === "working"} type="submit">Evaluate access</button>
      </form>
      {result ? (
        <dl className="authz-admin-result">
          <div><dt>Decision</dt><dd data-decision={result.evaluation.decision}>{result.evaluation.decision}</dd></div>
          <div><dt>Reason</dt><dd>{humanize(result.evaluation.reason_code)}</dd></div>
          <div><dt>Policy revision</dt><dd>{result.policy_revision}</dd></div>
        </dl>
      ) : null}
      <ActionMessage notice={notice} />
    </section>
  );
}

function DenialExplanationTool({ fixtureMode }: { fixtureMode: DevFixtureMode }) {
  const [traceId, setTraceId] = useState("");
  const [result, setResult] = useState<AuthzDenialExplanationResponse | null>(null);
  const [notice, setNotice] = useState<ActionNotice>(EMPTY_NOTICE);

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice({ tone: "working", message: "Reading bounded denial evidence…", traceId: "" });
    try {
      const response = fixtureMode
        ? denialFixture(traceId)
        : await explainAuthzDenial(traceId);
      setResult(response);
      setNotice({ tone: "success", message: "Denial evidence loaded without exposing matching rules or identities.", traceId: response.trace_id });
    } catch (error) {
      setResult(null);
      setNotice(actionFailure(error, "The denial trace is unavailable or expired."));
    }
  }

  return (
    <section className="authz-admin-tool-card">
      <header>
        <SearchCheck size={20} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Support-reader boundary</span>
          <h2>Explain a denial</h2>
        </div>
      </header>
      <p>
        A denied operator can share a trace ID with a separately authorized
        support reader. The result contains only the supplied scope, bounded
        reason, and policy provenance.
      </p>
      <form className="authz-admin-form authz-admin-trace-form" onSubmit={submit}>
        <label><span>Trace ID</span><input required value={traceId} onChange={(event) => setTraceId(event.target.value)} /></label>
        <button className="button" disabled={notice.tone === "working"} type="submit">Explain denial</button>
      </form>
      {result ? (
        <dl className="authz-admin-result">
          <div><dt>Reason</dt><dd>{humanize(result.reason_code)}</dd></div>
          <div><dt>Action</dt><dd><code>{result.action}</code></dd></div>
          <div><dt>Scope</dt><dd>{result.product} / {result.context} / {result.target_scope}</dd></div>
          <div><dt>Policy revision</dt><dd>{result.policy_revision}</dd></div>
        </dl>
      ) : null}
      <ActionMessage notice={notice} />
    </section>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function ActionMessage({ notice }: { notice: ActionNotice }) {
  if (notice.tone === "idle") return null;
  return (
    <div className="authz-admin-action-message" data-tone={notice.tone} role="status">
      <span>{notice.message}</span>
      {notice.traceId ? <code>{notice.traceId}</code> : null}
    </div>
  );
}

function actionFailure(error: unknown, fallback: string): ActionNotice {
  return error instanceof LaunchplaneApiError
    ? { tone: "error", message: error.message, traceId: error.traceId }
    : { tone: "error", message: error instanceof Error ? error.message : fallback, traceId: "" };
}

function principalCountSummary(counts: AuthzPolicyAdministrationReadResponse["principal_rule_counts"]): string {
  return [
    [counts.github_humans, "human"],
    [counts.github_actions, "workflow"],
    [counts.terminal_agents, "agent"],
    [counts.local_operators + counts.local_admins, "local"],
  ]
    .filter(([count]) => Number(count) > 0)
    .map(([count, label]) => `${count} ${label}`)
    .join(" · ") || "No principal rules";
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function yesNo(value: boolean): string {
  return value ? "Yes" : "No";
}

function downloadJson(filename: string, payload: AuthzActivePolicyExportResponse) {
  const url = URL.createObjectURL(
    new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function authzAdministrationFixture(mode: DevFixtureMode): AuthzAdministrationWorkspace {
  if (mode === "error") {
    throw new LaunchplaneApiError(
      "Authorization administration evidence is unavailable.",
      503,
      "launchplane_req_authz_fixture_error",
      "authz_policy_unavailable",
    );
  }
  const empty = mode === "empty";
  const principalRuleCounts = {
    github_actions: empty ? 0 : 2,
    github_humans: empty ? 0 : 2,
    terminal_agents: 0,
    local_operators: 0,
    local_admins: 0,
  };
  const policy = {
    record_id: "launchplane-authz-policy-r00000000000000000367-example",
    revision: 367,
    status: "active" as const,
    source: "managed_rule_set_reconcile",
    updated_at: "2026-08-27T02:00:00Z",
    policy_sha256: "a".repeat(64),
    schema_version: 2 as const,
  };
  const administration: AuthzPolicyAdministrationReadResponse = {
    status: "ok",
    trace_id: "launchplane_req_authz_fixture",
    policy,
    principal_rule_counts: principalRuleCounts,
    health: {
      state: empty ? "blocked" : "healthy",
      reason_codes: empty ? ["authz_policy_admin_unreachable"] : [],
      managed_rule_count: empty ? 0 : 4,
      unmanaged_rule_count: 0,
      github_actions_legacy_name_only_rule_count: 0,
      github_actions_privileged_unpinned_reusable_rule_count: 0,
    },
    managed_sets: {
      total_count: empty ? 0 : 2,
      returned_count: empty ? 0 : 2,
      truncated: false,
      items: empty
        ? []
        : [
            { managed_set_id: "owner.policy-admin", rule_count: 2, principal_rule_counts: { ...principalRuleCounts, github_actions: 0 } },
            { managed_set_id: "workload.preview", rule_count: 2, principal_rule_counts: { ...principalRuleCounts, github_humans: 0 } },
          ],
    },
    reachable_administrators: {
      policy_reachable: !empty,
      rule_count: empty ? 0 : 2,
      managed_rule_count: empty ? 0 : 2,
      unmanaged_rule_count: 0,
      principal_rule_counts: { ...principalRuleCounts, github_actions: 0 },
      caller_has_policy_administration: !empty,
      independent_from_caller_reachable: !empty,
      independent_from_caller_rule_count: empty ? 0 : 1,
    },
    managed_rules: {
      total_count: empty ? 0 : 2,
      returned_count: empty ? 0 : 2,
      truncated: false,
      items: empty
        ? []
        : [
            { managed_set_id: "owner.policy-admin", managed_rule_id: "primary-admin", principal_type: "github_humans", rule_sha256: "b".repeat(64) },
            { managed_set_id: "owner.policy-admin", managed_rule_id: "independent-admin", principal_type: "github_humans", rule_sha256: "c".repeat(64) },
          ],
    },
  };
  return {
    administration,
    history: {
      status: "ok",
      trace_id: "launchplane_req_authz_history_fixture",
      returned_count: 2,
      truncated: false,
      revisions: [
        { policy, audit: { audit_present: true, audit_sha256: "d".repeat(64), operation: "managed_rule_set_reconcile", mode: "apply", managed_set_id: "owner.policy-admin", changed: true, diff_counts: { updated_rule_count: 1 } } },
        { policy: { ...policy, record_id: "launchplane-authz-policy-r00000000000000000366-example", revision: 366, status: "superseded", policy_sha256: "e".repeat(64) }, audit: { audit_present: true, audit_sha256: "f".repeat(64), operation: "managed_rule_set_reconcile", mode: "apply", managed_set_id: "owner.policy-admin", changed: true, diff_counts: { added_rule_count: 1 } } },
      ],
    },
  };
}

function effectiveAccessFixture(request: EffectiveAccessEvaluateRequest): EffectiveAccessEvaluateResponse {
  return {
    status: "ok",
    trace_id: "launchplane_req_effective_access_fixture",
    policy_record_id: "launchplane-authz-policy-r00000000000000000367-example",
    policy_revision: 367,
    policy_sha256: "a".repeat(64),
    request: {
      action: request.action,
      product: request.product,
      context: request.context,
      target_scope: request.target_scope,
      instance: request.instance ?? "",
      principal_type: request.principal.principal_type,
    },
    evaluation: { decision: "allowed", reason_code: "allowed" },
  };
}

function denialFixture(traceId: string): AuthzDenialExplanationResponse {
  return {
    status: "ok",
    trace_id: traceId || "launchplane_req_denial_fixture",
    recorded_at: "2026-08-27T02:00:00Z",
    route_path: "/v1/example",
    principal_type: "github_human",
    action: "example.read",
    product: "launchplane",
    context: "launchplane",
    target_scope: "context",
    instance_specified: false,
    reason_code: "no_matching_grant",
    policy_record_id: "launchplane-authz-policy-r00000000000000000367-example",
    policy_revision: 367,
    policy_sha256: "a".repeat(64),
  };
}

function fixtureDelay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(resolve, 90);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Fixture request aborted.", "AbortError"));
      },
      { once: true },
    );
  });
}
