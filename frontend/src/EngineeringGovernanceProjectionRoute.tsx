import { History, Search, ShieldCheck } from "lucide-react";
import { useCallback, useMemo, useState, type FormEvent } from "react";

import { readGovernanceProjection } from "./api";
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
import { StatusIcon } from "./status-ui";
import type { Status } from "./types";

import type {
  GovernanceProjection,
  GovernanceProjectionResponse,
  MergeReadinessResult,
  OwnerAcceptanceProductDecision,
} from "./generated/openapi.ts";

interface GovernanceLookup {
  repository: string;
  pullRequestNumber: string;
  baseBranch: string;
}

const EMPTY_LOOKUP: GovernanceLookup = {
  repository: "",
  pullRequestNumber: "",
  baseBranch: "main",
};

const FIXTURE_LOOKUP: GovernanceLookup = {
  repository: "example/tenant-site",
  pullRequestNumber: "308",
  baseBranch: "main",
};

export function EngineeringGovernanceProjectionRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const initialLookup = useMemo(
    () => governanceLookup(window.location.search, fixtureMode),
    [fixtureMode],
  );
  const [draftLookup, setDraftLookup] = useState(initialLookup);
  const [submittedLookup, setSubmittedLookup] = useState<GovernanceLookup | null>(
    validLookup(initialLookup) ? initialLookup : null,
  );
  const [validationError, setValidationError] = useState("");

  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<GovernanceProjectionResponse> => {
      if (!submittedLookup) {
        throw new Error("Submit a pull request before loading governance evidence.");
      }
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.governanceProjectionForFixture(fixtureMode);
      }
      return readGovernanceProjection(
        submittedLookup.repository,
        Number(submittedLookup.pullRequestNumber),
        submittedLookup.baseBranch,
        signal,
      );
    },
    [fixtureMode, submittedLookup],
  );
  const resource = useEngineeringResource(
    loader,
    submittedLookup
      ? `governance:${submittedLookup.repository}:${submittedLookup.pullRequestNumber}:${submittedLookup.baseBranch}:${fixtureMode}`
      : "governance:empty",
    submittedLookup !== null,
  );

  function submitLookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = normalizeLookup(draftLookup);
    if (!validLookup(normalized)) {
      setValidationError("Enter owner/repository, a positive PR number, and a base branch.");
      return;
    }
    setValidationError("");
    setDraftLookup(normalized);
    setSubmittedLookup(normalized);
  }

  return (
    <EngineeringRouteFrame
      actions={
        submittedLookup ? (
          <EngineeringResourceControls
            cancel={resource.cancel}
            refresh={resource.refresh}
            refreshLabel="Refresh governance"
            state={resource.state}
          />
        ) : undefined
      }
      description="Inspect historical Owner product judgment, current ephemeral merge readiness, immutable admission, separate landing outcome, and neutral advisory observations for one pull request."
      icon={ShieldCheck}
      title="Governance evidence"
      view="governance-projection"
    >
      <EngineeringBoundaryNote title="Independent evidence — no fused approval verdict">
        Level 1 records product judgment and exposes <code>authorizes: []</code>.
        Level 2 remains <code>mode: ephemeral</code>, <code>authoritative: false</code>,
        and authorizes no effect. Level 3 admits one exact attempt; it does not mean
        the provider effect landed. Landing outcome is an independent durable fact.
        Advisory GitHub checks are neutral visibility only.
      </EngineeringBoundaryNote>

      <GovernanceLookupForm
        lookup={draftLookup}
        validationError={validationError}
        onChange={(field, value) =>
          setDraftLookup((current) => ({ ...current, [field]: value }))
        }
        onSubmit={submitLookup}
      />

      {submittedLookup ? (
        <EngineeringResourceGate
          noun="Governance evidence"
          refresh={resource.refresh}
          state={resource.state}
        >
          {(data) => <GovernanceWorkbench projection={data.projection} />}
        </EngineeringResourceGate>
      ) : (
        <EngineeringEmpty
          detail="Enter the repository, pull request number, and base branch to load one bounded read-only projection."
          icon={Search}
          title="Choose a pull request"
        />
      )}
    </EngineeringRouteFrame>
  );
}

function GovernanceLookupForm({
  lookup,
  validationError,
  onChange,
  onSubmit,
}: {
  lookup: GovernanceLookup;
  validationError: string;
  onChange: (field: keyof GovernanceLookup, value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="governance-lookup" onSubmit={onSubmit}>
      <label>
        Repository
        <input
          autoComplete="off"
          placeholder="owner/repository"
          value={lookup.repository}
          onChange={(event) => onChange("repository", event.currentTarget.value)}
        />
      </label>
      <label>
        Pull request
        <input
          inputMode="numeric"
          min="1"
          type="number"
          value={lookup.pullRequestNumber}
          onChange={(event) => onChange("pullRequestNumber", event.currentTarget.value)}
        />
      </label>
      <label>
        Base branch
        <input
          autoComplete="off"
          value={lookup.baseBranch}
          onChange={(event) => onChange("baseBranch", event.currentTarget.value)}
        />
      </label>
      <button className="button primary" type="submit">
        <Search size={15} aria-hidden="true" />
        Load evidence
      </button>
      {validationError ? <p role="alert">{validationError}</p> : null}
    </form>
  );
}

function GovernanceWorkbench({ projection }: { projection: GovernanceProjection }) {
  return (
    <article className="governance-workbench" aria-label="Independent governance evidence">
      <GovernanceOwnerFacet projection={projection} />
      <GovernanceReadinessFacet readiness={projection.merge_readiness.result} projection={projection} />
      <GovernanceAdmissionFacet projection={projection} />
      <GovernanceLandingFacet projection={projection} />
      <GovernanceAdvisoryFacet projection={projection} />
    </article>
  );
}

function GovernanceOwnerFacet({ projection }: { projection: GovernanceProjection }) {
  const owner = projection.owner_judgment;
  return (
    <section className="governance-facet" aria-label="Level 1 historical Owner judgment">
      <GovernanceFacetHeader
        eyebrow="Level 1 · historical"
        label="Owner product judgment"
        status={ownerTone(owner.current.status)}
        value={humanize(owner.current.status)}
      />
      <p className="governance-authority-note">
        Owner <code>accepted</code> means product judgment only. <code>authorizes: []</code>.
        It never means merge-ready, admitted, landed, release-authorized, or
        production-authorized.
      </p>
      <dl className="governance-meta-grid">
        <div>
          <dt>Current reason</dt>
          <dd><code>{owner.current.reason_code}</code></dd>
        </div>
        <div>
          <dt>Human semantics</dt>
          <dd><code>{owner.current.human_action_semantics}</code></dd>
        </div>
        <div>
          <dt>History events</dt>
          <dd>{owner.history.length}</dd>
        </div>
      </dl>
      <OwnerProductFacets products={owner.current.products} />
      <div className="governance-history" aria-label="Immutable Owner review history">
        <h3><History size={15} aria-hidden="true" /> Immutable event history</h3>
        {owner.history.length ? (
          <ol>
            {owner.history.map((entry) => (
              <li key={entry.record.event_id}>
                <strong>{humanize(entry.record.action)}</strong>
                <span>{entry.record.binding.product}</span>
                <time dateTime={entry.record.occurred_at}>{formatTime(entry.record.occurred_at)}</time>
                <code>{entry.human_action_semantics}</code>
                <code>{entry.target_status} target</code>
                <code>{entry.decision_relationship} decision</code>
              </li>
            ))}
          </ol>
        ) : (
          <p>No Owner review event has been recorded for this exact change.</p>
        )}
      </div>
    </section>
  );
}

function OwnerProductFacets({ products }: { products: OwnerAcceptanceProductDecision[] }) {
  if (!products.length) return null;
  return (
    <ul className="governance-product-list" aria-label="Per-product Owner judgments">
      {products.map((product) => (
        <li key={`${product.product}:${product.system}:${product.action}:${product.environment}`}>
          <span className="engineering-status-chip" data-status={ownerTone(product.status)}>
            <StatusIcon status={ownerTone(product.status)} />
            {humanize(product.status)}
          </span>
          <div>
            <strong>{product.product}</strong>
            <span>{product.system} · {product.action} · {product.environment}</span>
            <code>{product.reason_code}</code>
          </div>
        </li>
      ))}
    </ul>
  );
}

function GovernanceReadinessFacet({
  readiness,
  projection,
}: {
  readiness: MergeReadinessResult | null;
  projection: GovernanceProjection;
}) {
  if (!readiness) {
    return (
      <section className="governance-facet" aria-label="Level 2 current merge readiness">
        <GovernanceFacetHeader
          eyebrow="Level 2 · current"
          label="Ephemeral merge readiness"
          status={projection.merge_readiness.availability === "not_active" ? "skipped" : "unknown"}
          value={humanize(projection.merge_readiness.availability)}
        />
        <p className="governance-authority-note">
          No reusable authority exists. Reason: <code>{projection.merge_readiness.reason_code}</code>.
        </p>
      </section>
    );
  }
  const facets = [
    ["Technical checks", readiness.technical_checks.state, readiness.technical_checks.reason_codes],
    ["Engineering review", readiness.engineering_review.state, readiness.engineering_review.reason_codes],
    ["Policy", readiness.policy.state, readiness.policy.reason_codes],
    ["Candidate identity", readiness.candidate.state, readiness.candidate.reason_codes],
    ["Controller fence", readiness.fence.state, readiness.fence.reason_codes],
  ] as const;
  return (
    <section className="governance-facet" aria-label="Level 2 current merge readiness">
      <GovernanceFacetHeader
        eyebrow="Level 2 · current"
        label="Ephemeral merge readiness"
        status={readinessTone(readiness.state)}
        value={humanize(readiness.state)}
      />
      <p className="governance-authority-note">
        <code>mode: ephemeral</code>, <code>authoritative: false</code>, <code>authorizes: []</code>.
        The word ready belongs only to this current machine-derived layer.
      </p>
      <div className="governance-readiness-grid">
        <ReadinessSubfacet
          label="Owner evidence"
          state={worstOwnerState(readiness)}
          reasons={readiness.owner_facets.flatMap((facet) => facet.reason_codes)}
        />
        {facets.map(([label, state, reasons]) => (
          <ReadinessSubfacet key={label} label={label} state={state} reasons={reasons} />
        ))}
      </div>
    </section>
  );
}

function ReadinessSubfacet({
  label,
  state,
  reasons,
}: {
  label: string;
  state: string;
  reasons: readonly string[];
}) {
  return (
    <div className="governance-subfacet">
      <span className="engineering-status-chip" data-status={readinessTone(state)}>
        <StatusIcon status={readinessTone(state)} />
        {humanize(state)}
      </span>
      <strong>{label}</strong>
      <ul>{reasons.map((reason, index) => <li key={`${reason}:${index}`}><code>{reason}</code></li>)}</ul>
    </div>
  );
}

function GovernanceAdmissionFacet({ projection }: { projection: GovernanceProjection }) {
  const admission = projection.merge_admission;
  const status =
    admission.status === "admitted_current_target"
      ? "Recorded for current target"
      : admission.status === "admitted_historical_target"
        ? "Historical admission"
        : "No admission recorded";
  return (
    <section className="governance-facet" aria-label="Level 3 immutable merge admission">
      <GovernanceFacetHeader
        eyebrow="Level 3 · immutable attempt"
        label="Merge admission"
        status={admission.status === "not_recorded" ? "skipped" : "unknown"}
        value={status}
      />
      <p className="governance-authority-note">
        Admission authorized one exact provider-effect attempt at record creation. It is
        immutable history, grants no current effect authority, and does not mean the pull
        request landed.
      </p>
      {admission.record ? (
        <dl className="governance-meta-grid">
          <div><dt>Attempt</dt><dd><code>{admission.record.attempt_id}</code></dd></div>
          <div><dt>Candidate</dt><dd><code>{shortSha(admission.record.candidate_sha)}</code></dd></div>
          <div><dt>Recorded</dt><dd>{formatTime(admission.record.created_at)}</dd></div>
        </dl>
      ) : null}
    </section>
  );
}

function GovernanceLandingFacet({ projection }: { projection: GovernanceProjection }) {
  const landing = projection.landing_outcome;
  return (
    <section className="governance-facet" aria-label="Separate landing outcome">
      <GovernanceFacetHeader
        eyebrow="Provider observation · durable"
        label="Landing outcome"
        status={landingTone(landing.status)}
        value={`${humanize(landing.status)} · ${humanize(landing.target_status)} target`}
      />
      <p className="governance-authority-note">
        Landed, rejected, and reconcile required are independent observations. Missing
        outcome evidence is never interpreted as landed.
      </p>
      {landing.record ? (
        <dl className="governance-meta-grid">
          <div><dt>Reason</dt><dd><code>{landing.record.reason}</code></dd></div>
          <div><dt>Observed</dt><dd>{formatTime(landing.record.observed_at)}</dd></div>
          <div><dt>Merge commit</dt><dd><code>{shortSha(landing.record.merge_commit_sha)}</code></dd></div>
        </dl>
      ) : null}
    </section>
  );
}

function GovernanceAdvisoryFacet({ projection }: { projection: GovernanceProjection }) {
  return (
    <section
      className="governance-facet governance-advisory"
      aria-label="Neutral advisory observations"
      data-advisory="true"
    >
      <GovernanceFacetHeader
        eyebrow="Visibility only"
        label="Advisory GitHub observations"
        status="skipped"
        value="Neutral · non-blocking"
      />
      <p className="governance-authority-note">
        These check observations are explicitly neutral, non-authoritative, and excluded
        from required technical checks.
      </p>
      {projection.advisory_observations.length ? (
        <ul className="governance-advisory-list">
          {projection.advisory_observations.map((entry) => (
            <li
              key={`${entry.observation_scope}:${entry.observation.name}:${entry.observation.app_id ?? 0}:${entry.observation.state}`}
            >
              <strong>{entry.observation.name}</strong>
              <span>{humanize(entry.observation.state)}</span>
              <code>{entry.observation_scope}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p>No advisory check observation is attached to the current or admitted readiness evidence.</p>
      )}
    </section>
  );
}

function GovernanceFacetHeader({
  eyebrow,
  label,
  status,
  value,
}: {
  eyebrow: string;
  label: string;
  status: Status;
  value: string;
}) {
  return (
    <header className="governance-facet-header">
      <div>
        <span>{eyebrow}</span>
        <h2>{label}</h2>
      </div>
      <span className="engineering-status-chip" data-status={status}>
        <StatusIcon status={status} />
        {value}
      </span>
    </header>
  );
}

function governanceLookup(search: string, fixtureMode: DevFixtureMode): GovernanceLookup {
  const params = new URLSearchParams(search);
  if (fixtureMode) return FIXTURE_LOOKUP;
  return normalizeLookup({
    repository: params.get("repository") ?? EMPTY_LOOKUP.repository,
    pullRequestNumber: params.get("pull_request_number") ?? EMPTY_LOOKUP.pullRequestNumber,
    baseBranch: params.get("base_branch") ?? EMPTY_LOOKUP.baseBranch,
  });
}

function normalizeLookup(lookup: GovernanceLookup): GovernanceLookup {
  return {
    repository: lookup.repository.trim().toLowerCase(),
    pullRequestNumber: lookup.pullRequestNumber.trim(),
    baseBranch: lookup.baseBranch.trim(),
  };
}

function validLookup(lookup: GovernanceLookup): boolean {
  return (
    /^[^/\s]+\/[^/\s]+$/.test(lookup.repository) &&
    Number.isInteger(Number(lookup.pullRequestNumber)) &&
    Number(lookup.pullRequestNumber) > 0 &&
    Boolean(lookup.baseBranch)
  );
}

function ownerTone(status: string): Status {
  if (status === "accepted") return "pass";
  if (status === "changes_requested" || status === "revoked") return "fail";
  if (status === "not_required") return "skipped";
  return "unknown";
}

function readinessTone(state: string): Status {
  if (state === "ready") return "pass";
  if (state.startsWith("blocked")) return "blocked";
  return "unknown";
}

function landingTone(status: string): Status {
  if (status === "landed") return "pass";
  if (status === "rejected") return "fail";
  if (status === "reconcile_required") return "blocked";
  return "skipped";
}

function worstOwnerState(readiness: MergeReadinessResult): string {
  return readiness.owner_facets.find((facet) => facet.state !== "ready")?.state ?? "ready";
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function shortSha(value: string): string {
  return value ? value.slice(0, 12) : "Not recorded";
}
