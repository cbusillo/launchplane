import {
  Bot,
  CheckCircle2,
  ExternalLink,
  GitPullRequest,
  ListChecks,
  Search,
  ShieldCheck,
  UserRoundCheck,
  Wrench,
} from "lucide-react";
import { useCallback, useMemo, useState, type FormEvent } from "react";

import { readTenantAdmissionEvaluation } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
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
import { safeExternalUrl } from "./url";

import type {
  TenantAdmissionEvaluationReadModel,
  TenantAdmissionEvaluationReadResponse,
  TenantAdmissionHumanActionReadModel,
  TenantAdmissionPathResult,
  TenantAdmissionTechnicalChecks,
} from "./generated/openapi.ts";

interface TenantAdmissionLookup {
  product: string;
  context: string;
  repositoryId: string;
  repositoryOwnerId: string;
  repository: string;
  pullRequestNumber: string;
  headSha: string;
  baseBranch: string;
  mergeMethod: "merge" | "squash" | "rebase";
}

const EMPTY_LOOKUP: TenantAdmissionLookup = {
  product: "",
  context: "",
  repositoryId: "",
  repositoryOwnerId: "",
  repository: "",
  pullRequestNumber: "",
  headSha: "",
  baseBranch: "main",
  mergeMethod: "merge",
};

const FIXTURE_LOOKUP: TenantAdmissionLookup = {
  product: "example-site",
  context: "example-site",
  repositoryId: "1001",
  repositoryOwnerId: "2001",
  repository: "example/tenant-site",
  pullRequestNumber: "69",
  headSha: "a".repeat(40),
  baseBranch: "main",
  mergeMethod: "merge",
};

export function EngineeringTenantAdmissionRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const initialLookup = useMemo(
    () => tenantAdmissionLookup(window.location.search, fixtureMode),
    [fixtureMode],
  );
  const [draftLookup, setDraftLookup] = useState(initialLookup);
  const [submittedLookup, setSubmittedLookup] = useState<TenantAdmissionLookup | null>(
    completeLookup(initialLookup) ? initialLookup : null,
  );
  const [validationError, setValidationError] = useState("");

  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<TenantAdmissionEvaluationReadResponse> => {
      if (!submittedLookup) {
        throw new Error("Submit an exact pull request before loading admission evidence.");
      }
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.tenantAdmissionForFixture(fixtureMode);
      }
      return readTenantAdmissionEvaluation(
        {
          query: {
            product: submittedLookup.product,
            context: submittedLookup.context,
            repository_id: submittedLookup.repositoryId,
            repository_owner_id: submittedLookup.repositoryOwnerId,
            repository: submittedLookup.repository,
            pull_request_number: Number(submittedLookup.pullRequestNumber),
            head_sha: submittedLookup.headSha,
            base_branch: submittedLookup.baseBranch,
            merge_method: submittedLookup.mergeMethod,
          },
        },
        signal,
      );
    },
    [fixtureMode, submittedLookup],
  );
  const resource = useEngineeringResource(
    loader,
    submittedLookup ? lookupKey(submittedLookup, fixtureMode) : "tenant-admission:empty",
    submittedLookup !== null,
  );

  function submitLookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const error = validateLookup(draftLookup);
    if (error) {
      setValidationError(error);
      return;
    }
    const normalizedLookup = normalizeLookup(draftLookup);
    setValidationError("");
    setSubmittedLookup(normalizedLookup);
    writeLookupToLocation(normalizedLookup);
  }

  function updateLookup(field: keyof TenantAdmissionLookup, value: string) {
    setDraftLookup((current) => ({ ...current, [field]: value }));
  }

  return (
    <EngineeringRouteFrame
      actions={
        submittedLookup ? (
          <EngineeringResourceControls
            cancel={resource.cancel}
            refresh={resource.refresh}
            refreshLabel="Refresh evaluation"
            state={resource.state}
          />
        ) : undefined
      }
      description="Inspect one exact pull-request head, its DB-backed repository classification, all admission paths, GitHub mergeability, and required technical checks without granting browser write authority."
      icon={ShieldCheck}
      title="Tenant admission"
      view="tenant-admission"
    >
      <EngineeringBoundaryNote title="One human gate, no agent waiver authority">
        Tenant UI work needs manager preview approval, a reasoned repository-owner
        technical waiver, or explicit trusted-maintenance evidence. Engineering
        repositories retain normal flow. This browser route can explain those paths;
        it cannot approve, waive, delegate, reconcile, or merge.
      </EngineeringBoundaryNote>

      <TenantAdmissionLookupForm
        error={validationError}
        lookup={draftLookup}
        onSubmit={submitLookup}
        updateLookup={updateLookup}
      />

      {submittedLookup ? (
        <EngineeringResourceGate
          noun="tenant admission evaluation"
          refresh={resource.refresh}
          state={resource.state}
        >
          {(response) => <TenantAdmissionEvaluation readModel={response.read_model} />}
        </EngineeringResourceGate>
      ) : (
        <EngineeringEmpty
          detail="Launchplane needs the exact numeric repository identity, pull-request number, current head SHA, and base branch. It will not infer tenant status from a repository name, label, file path, or bot author."
          icon={Search}
          title="Choose an exact pull request"
        />
      )}
    </EngineeringRouteFrame>
  );
}

function TenantAdmissionLookupForm({
  error,
  lookup,
  onSubmit,
  updateLookup,
}: {
  error: string;
  lookup: TenantAdmissionLookup;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  updateLookup: (field: keyof TenantAdmissionLookup, value: string) => void;
}) {
  return (
    <form className="tenant-admission-lookup" onSubmit={onSubmit}>
      <header>
        <div>
          <span className="engineering-kicker">Exact candidate</span>
          <h2>Inspect a pull request</h2>
          <p>
            Values are routing and identity evidence only. The active Launchplane
            classification record remains authority.
          </p>
        </div>
        <button className="button primary" type="submit">
          <Search size={15} aria-hidden="true" />
          Evaluate
        </button>
      </header>
      <div className="tenant-admission-fields">
        <LookupField
          label="Repository"
          value={lookup.repository}
          placeholder="owner/repository"
          onChange={(value) => updateLookup("repository", value)}
        />
        <LookupField
          label="Pull request"
          value={lookup.pullRequestNumber}
          inputMode="numeric"
          placeholder="69"
          onChange={(value) => updateLookup("pullRequestNumber", value)}
        />
        <LookupField
          label="Repository ID"
          value={lookup.repositoryId}
          inputMode="numeric"
          placeholder="Numeric GitHub ID"
          onChange={(value) => updateLookup("repositoryId", value)}
        />
        <LookupField
          label="Owner ID"
          value={lookup.repositoryOwnerId}
          inputMode="numeric"
          placeholder="Numeric GitHub owner ID"
          onChange={(value) => updateLookup("repositoryOwnerId", value)}
        />
        <LookupField
          label="Product"
          value={lookup.product}
          placeholder="Launchplane product key"
          onChange={(value) => updateLookup("product", value)}
        />
        <LookupField
          label="Context"
          value={lookup.context}
          placeholder="Launchplane context"
          onChange={(value) => updateLookup("context", value)}
        />
        <LookupField
          className="tenant-admission-head-field"
          label="Exact head SHA"
          value={lookup.headSha}
          placeholder="40 or 64 lowercase hex characters"
          onChange={(value) => updateLookup("headSha", value)}
        />
        <LookupField
          label="Base branch"
          value={lookup.baseBranch}
          placeholder="main"
          onChange={(value) => updateLookup("baseBranch", value)}
        />
        <label>
          <span>Merge method</span>
          <select
            value={lookup.mergeMethod}
            onChange={(event) => updateLookup("mergeMethod", event.target.value)}
          >
            <option value="merge">Merge commit</option>
            <option value="squash">Squash</option>
            <option value="rebase">Rebase</option>
          </select>
        </label>
      </div>
      {error ? <p className="tenant-admission-form-error" role="alert">{error}</p> : null}
    </form>
  );
}

function LookupField({
  className = "",
  inputMode,
  label,
  onChange,
  placeholder,
  value,
}: {
  className?: string;
  inputMode?: "numeric" | "text";
  label: string;
  onChange: (value: string) => void;
  placeholder: string;
  value: string;
}) {
  return (
    <label className={className}>
      <span>{label}</span>
      <input
        inputMode={inputMode}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function TenantAdmissionEvaluation({
  readModel,
}: {
  readModel: TenantAdmissionEvaluationReadModel;
}) {
  const evaluation = readModel.evaluation;
  const admission = evaluation.admission;
  const checks = evaluation.technical_checks;
  const pullRequestUrl = safeExternalUrl(evaluation.pull_request_facts.pull_request_url);
  const trustedMaintenance = admission?.paths.trusted_maintenance ?? null;
  return (
    <div className="tenant-admission-result">
      <section
        className="tenant-admission-summary"
        data-tone={evaluationTone(readModel)}
      >
        <div className="tenant-admission-summary-icon" aria-hidden="true">
          {evaluation.outcome === "ready" ? (
            <CheckCircle2 size={24} />
          ) : (
            <ShieldCheck size={24} />
          )}
        </div>
        <div>
          <span className="engineering-kicker">Exact-head decision</span>
          <h2>{evaluationTitle(readModel)}</h2>
          <p>{evaluation.detail}</p>
        </div>
        <StatePill state={evaluation.outcome} />
      </section>

      <div className="engineering-metric-grid tenant-admission-metrics">
        <Metric
          label="Classification"
          value={admission?.classification_kind || "Unavailable"}
          tone={classificationTone(admission?.classification_kind ?? "")}
        />
        <Metric
          label="Admission"
          value={admission?.category ?? "Unavailable"}
          tone={admissionTone(admission?.category ?? "unavailable")}
        />
        <Metric
          label="Technical checks"
          value={checks?.status ?? "Not evaluated"}
          tone={checks?.status ?? "unavailable"}
        />
        <Metric
          label="Mergeability"
          value={mergeabilityLabel(evaluation.pull_request_facts)}
          tone={evaluation.pull_request_facts.mergeable === true ? "pass" : "blocked"}
        />
        <Metric
          label="Controller"
          value={evaluation.outcome}
          tone={evaluationTone(readModel)}
        />
      </div>

      {admission?.classification_kind === "tenant_ui" ? (
        <section className="tenant-admission-path-section" aria-labelledby="human-paths-title">
          <header>
            <div>
              <span className="engineering-kicker">Human paths</span>
              <h2 id="human-paths-title">One current human action is enough</h2>
            </div>
            <span className="tenant-admission-agent-boundary">
              <Bot size={15} aria-hidden="true" />
              Agent authoring disabled
            </span>
          </header>
          <div className="tenant-admission-action-grid">
            {readModel.human_actions.map((action) => (
              <HumanActionCard action={action} key={action.action_kind} />
            ))}
            <AutomaticPathCard path={trustedMaintenance} />
          </div>
        </section>
      ) : admission?.classification_kind === "engineering" ? (
        <EngineeringBoundaryNote title="No manager gate for engineering">
          This repository is classified as engineering. Launchplane reports normal
          flow and does not ask a manager or repository owner to approve the PR.
        </EngineeringBoundaryNote>
      ) : evaluation.outcome === "already_merged" ? (
        <EngineeringBoundaryNote title="Pull request already merged">
          This exact pull request is already on the target branch, so Launchplane
          does not offer an admission action for it.
        </EngineeringBoundaryNote>
      ) : (
        <EngineeringBoundaryNote title="Classification evidence unavailable">
          Launchplane cannot prove that this repository is engineering or tenant UI.
          The candidate remains blocked until the DB-backed repository classification
          and exact admission evidence are available.
        </EngineeringBoundaryNote>
      )}

      <div className="tenant-admission-detail-grid">
        <section className="tenant-admission-evidence-card">
          <header>
            <GitPullRequest size={18} aria-hidden="true" />
            <div>
              <span className="engineering-kicker">Exact binding</span>
              <h2>Pull-request identity</h2>
            </div>
            {pullRequestUrl ? (
              <a href={pullRequestUrl.toString()} target="_blank" rel="noreferrer">
                Open PR
                <ExternalLink size={14} aria-hidden="true" />
              </a>
            ) : null}
          </header>
          <EvidenceRows
            rows={[
              ["Repository", evaluation.candidate.repository],
              ["Repository ID", evaluation.candidate.repository_id],
              ["Owner ID", evaluation.candidate.repository_owner_id],
              ["Pull request", `#${evaluation.candidate.pull_request_number}`],
              ["Head SHA", evaluation.candidate.head_sha],
              ["Base", `${evaluation.base_branch} @ ${evaluation.pull_request_facts.base_sha}`],
              ["Decision binding", admission?.decision.decision_binding_sha256 || "Unavailable"],
            ]}
          />
        </section>

        <TechnicalChecksCard checks={checks} />
      </div>

      <footer className="tenant-admission-footer">
        <span>Evaluated {formatTime(readModel.generated_at)}</span>
        <span>
          {readModel.agent_authoring_allowed
            ? "Agent authoring enabled"
            : "Read-only evidence; no agent human-action authority"}
        </span>
      </footer>
    </div>
  );
}

function HumanActionCard({
  action,
}: {
  action: TenantAdmissionHumanActionReadModel;
}) {
  return (
    <article className="tenant-admission-action-card" data-state={action.availability}>
      <header>
        <span>
          {action.action_kind === "manager_preview_approval" ? (
            <UserRoundCheck size={18} aria-hidden="true" />
          ) : (
            <Wrench size={18} aria-hidden="true" />
          )}
          {action.title}
        </span>
        <StatePill state={action.path_state} />
      </header>
      <p>{action.detail}</p>
      <small>
        {action.agent_authoring_allowed
          ? "Agent may author"
          : "Requires an authorized human; agents can only explain this path."}
      </small>
    </article>
  );
}

function AutomaticPathCard({ path }: { path: TenantAdmissionPathResult | null }) {
  const state = path?.state ?? "unavailable";
  return (
    <article className="tenant-admission-action-card" data-state={state}>
      <header>
        <span>
          <ShieldCheck size={18} aria-hidden="true" />
          Trusted maintenance
        </span>
        <StatePill state={state} />
      </header>
      <p>
        Explicit numeric actor and event policy may admit routine maintenance. It
        is automatic evidence, not a human bypass and never a blanket bot rule.
      </p>
      <small>No manager or owner action is requested for this path.</small>
    </article>
  );
}

function TechnicalChecksCard({ checks }: { checks: TenantAdmissionTechnicalChecks | null }) {
  return (
    <section className="tenant-admission-evidence-card">
      <header>
        <ListChecks size={18} aria-hidden="true" />
        <div>
          <span className="engineering-kicker">Technical readiness</span>
          <h2>Required checks</h2>
        </div>
        <StatePill state={checks?.status ?? "unavailable"} />
      </header>
      {checks ? (
        <>
          <div className="tenant-admission-check-list">
            {checks.required_checks.map((requiredCheck) => {
              const matchingSignals = checks.signals.filter(
                (signal) =>
                  signal.name.toLowerCase() === requiredCheck.name.toLowerCase() &&
                  (requiredCheck.app_id === null || signal.app_id === requiredCheck.app_id),
              );
              const state = combinedSignalState(matchingSignals.map((signal) => signal.state));
              return (
                <div key={`${requiredCheck.name}:${requiredCheck.app_id ?? "any"}`}>
                  <span>{requiredCheck.name}</span>
                  <small>
                    {requiredCheck.app_id ? `App ${requiredCheck.app_id}` : "Any trusted app"}
                  </small>
                  <StatePill state={state} />
                </div>
              );
            })}
          </div>
          <div className="tenant-admission-check-meta">
            <span>{checks.strict ? "Strict base freshness" : "Non-strict base policy"}</span>
            <span>
              Base current: {checks.base_up_to_date === null ? "not required" : checks.base_up_to_date ? "yes" : "no"}
            </span>
            <code>{checks.binding_sha256}</code>
          </div>
        </>
      ) : (
        <EngineeringEmpty
          detail="Technical checks were not evaluated because the repository classification or pull-request mergeability was unavailable."
          icon={ListChecks}
          title="No current check evidence"
        />
      )}
    </section>
  );
}

function EvidenceRows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="tenant-admission-evidence-rows">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Metric({
  label,
  tone,
  value,
}: {
  label: string;
  tone: string;
  value: string;
}) {
  return (
    <div className="engineering-metric" data-tone={tone}>
      <span>{label}</span>
      <strong>{humanize(value)}</strong>
    </div>
  );
}

function StatePill({ state }: { state: string }) {
  return (
    <span className="tenant-admission-state" data-state={state}>
      {humanize(state)}
    </span>
  );
}

function humanize(value: string): string {
  const normalized = value.replaceAll("_", " ").trim();
  return normalized
    ? `${normalized.slice(0, 1).toUpperCase()}${normalized.slice(1)}`
    : "Unknown";
}

function evaluationTone(readModel: TenantAdmissionEvaluationReadModel): string {
  if (readModel.evaluation.outcome === "ready") {
    return "pass";
  }
  if (readModel.evaluation.outcome === "not_applicable") {
    return "neutral";
  }
  const admission = readModel.evaluation.admission;
  if (admission?.category === "pending" && readModel.evaluation.technical_checks?.status === "pass") {
    return "pending";
  }
  return "blocked";
}

function evaluationTitle(readModel: TenantAdmissionEvaluationReadModel): string {
  const evaluation = readModel.evaluation;
  if (evaluation.outcome === "ready") {
    return "Ready for controller merge";
  }
  if (evaluation.outcome === "already_merged") {
    return "Already merged";
  }
  if (evaluation.outcome === "not_applicable") {
    return "Engineering normal flow";
  }
  if (evaluation.admission?.category === "pending") {
    return "Waiting for one current admission path";
  }
  return "Merge remains blocked";
}

function classificationTone(classification: string): string {
  if (classification === "engineering") {
    return "pass";
  }
  if (classification === "tenant_ui") {
    return "pending";
  }
  return "blocked";
}

function admissionTone(category: string): string {
  if (
    ["engineering", "manager-approved", "technical-waived", "maintenance-admitted"].includes(
      category,
    )
  ) {
    return "pass";
  }
  return category === "pending" ? "pending" : "blocked";
}

function mergeabilityLabel(facts: TenantAdmissionEvaluationReadModel["evaluation"]["pull_request_facts"]): string {
  if (facts.merged) {
    return "Merged";
  }
  if (facts.draft) {
    return "Draft";
  }
  if (facts.mergeable === true) {
    return "Mergeable";
  }
  if (facts.mergeable === false) {
    return "Conflicting";
  }
  return "Unknown";
}

function combinedSignalState(
  states: Array<"pass" | "pending" | "fail" | "unavailable">,
): "pass" | "pending" | "fail" | "unavailable" {
  if (!states.length || states.includes("unavailable")) {
    return "unavailable";
  }
  if (states.includes("fail")) {
    return "fail";
  }
  if (states.includes("pending")) {
    return "pending";
  }
  return "pass";
}

function tenantAdmissionLookup(
  search: string,
  fixtureMode: DevFixtureMode,
): TenantAdmissionLookup {
  const params = new URLSearchParams(search);
  const lookup: TenantAdmissionLookup = {
    product: params.get("product")?.trim() ?? "",
    context: params.get("context")?.trim() ?? "",
    repositoryId: params.get("repository_id")?.trim() ?? "",
    repositoryOwnerId: params.get("repository_owner_id")?.trim() ?? "",
    repository: params.get("repository")?.trim() ?? "",
    pullRequestNumber: params.get("pull_request_number")?.trim() ?? "",
    headSha: params.get("head_sha")?.trim() ?? "",
    baseBranch: params.get("base_branch")?.trim() ?? "main",
    mergeMethod: mergeMethod(params.get("merge_method")),
  };
  return completeLookup(lookup) ? lookup : fixtureMode ? FIXTURE_LOOKUP : EMPTY_LOOKUP;
}

function completeLookup(lookup: TenantAdmissionLookup): boolean {
  return Boolean(
    lookup.product &&
      lookup.context &&
      lookup.repositoryId &&
      lookup.repositoryOwnerId &&
      lookup.repository &&
      lookup.pullRequestNumber &&
      lookup.headSha &&
      lookup.baseBranch,
  );
}

function validateLookup(lookup: TenantAdmissionLookup): string {
  if (!completeLookup(lookup)) {
    return "Every exact candidate field is required.";
  }
  if (!/^\d+$/.test(lookup.repositoryId) || !/^\d+$/.test(lookup.repositoryOwnerId)) {
    return "Repository and owner IDs must be positive numeric GitHub IDs.";
  }
  if (!/^\d+$/.test(lookup.pullRequestNumber) || Number(lookup.pullRequestNumber) < 1) {
    return "Pull request must be a positive number.";
  }
  if (!/^[0-9a-f]{40}([0-9a-f]{24})?$/.test(lookup.headSha.trim().toLowerCase())) {
    return "Head SHA must be a complete 40- or 64-character hexadecimal Git SHA.";
  }
  if (!lookup.repository.includes("/")) {
    return "Repository must use the OWNER/REPO form.";
  }
  return "";
}

function normalizeLookup(lookup: TenantAdmissionLookup): TenantAdmissionLookup {
  return {
    ...lookup,
    product: lookup.product.trim(),
    context: lookup.context.trim(),
    repositoryId: lookup.repositoryId.trim(),
    repositoryOwnerId: lookup.repositoryOwnerId.trim(),
    repository: lookup.repository.trim().toLowerCase(),
    pullRequestNumber: lookup.pullRequestNumber.trim(),
    headSha: lookup.headSha.trim().toLowerCase(),
    baseBranch: lookup.baseBranch.trim(),
  };
}

function lookupKey(lookup: TenantAdmissionLookup, fixtureMode: DevFixtureMode): string {
  return [
    "tenant-admission",
    fixtureMode,
    lookup.repositoryId,
    lookup.pullRequestNumber,
    lookup.headSha,
    lookup.baseBranch,
  ].join(":");
}

function writeLookupToLocation(lookup: TenantAdmissionLookup) {
  const params = new URLSearchParams(window.location.search);
  params.set("product", lookup.product);
  params.set("context", lookup.context);
  params.set("repository_id", lookup.repositoryId);
  params.set("repository_owner_id", lookup.repositoryOwnerId);
  params.set("repository", lookup.repository);
  params.set("pull_request_number", lookup.pullRequestNumber);
  params.set("head_sha", lookup.headSha);
  params.set("base_branch", lookup.baseBranch);
  params.set("merge_method", lookup.mergeMethod);
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
}

function mergeMethod(value: string | null): TenantAdmissionLookup["mergeMethod"] {
  return value === "squash" || value === "rebase" ? value : "merge";
}
