import {
  Activity,
  ExternalLink,
  GitMerge,
  ListChecks,
  Route,
  ShieldAlert,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  readMergeTrainControllerStatus,
  readMergeTrainPolicyTargets,
} from "./api";
import {
  MERGE_TRAIN_BROWSER_BOUNDARY,
  mergeTrainControllerTone,
  mergeTrainStatusScopeKey,
  mergeTrainTargetSelection,
  mergeTrainTargetOptions,
  type MergeTrainTargetOption,
} from "./engineering-model";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
  type EngineeringResourceController,
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
import { safeExternalUrl } from "./url";

import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type {
  MergeTrainControllerRecordSummary,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
} from "./generated/openapi.ts";

export function EngineeringMergeTrainRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const policyLoader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<MergeTrainPolicyTargetsResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.mergeTrainTargetsForFixture(fixtureMode);
      }
      return readMergeTrainPolicyTargets(signal);
    },
    [fixtureMode],
  );
  const policyResource = useEngineeringResource(
    policyLoader,
    `merge-train-targets:${fixtureMode}`,
  );
  const targets = useMemo(
    () => mergeTrainTargetOptions(policyResource.state.data?.targets ?? []),
    [policyResource.state.data?.targets],
  );
  const [targetSelection, setTargetSelection] = useState(() =>
    mergeTrainTargetSelection(window.location.search),
  );
  const selectedKey = targetSelection.key;

  useEffect(() => {
    if (!selectedKey && !targetSelection.invalid && targets.length === 1) {
      setTargetSelection({ invalid: false, key: targets[0].key });
      writeTargetToLocation(targets[0]);
    }
  }, [selectedKey, targetSelection.invalid, targets]);

  const selectedTarget =
    targets.find((target) => target.key === selectedKey) ?? null;
  const statusLoader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<MergeTrainControllerStatusResponse> => {
      if (!selectedTarget) {
        throw new Error("No merge-train policy target is selected.");
      }
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.mergeTrainStatusForFixture(
          fixtureMode,
          selectedTarget.repository,
          selectedTarget.baseBranch,
        );
      }
      return readMergeTrainControllerStatus(
        selectedTarget.repository,
        selectedTarget.baseBranch,
        signal,
      );
    },
    [fixtureMode, selectedTarget],
  );
  const statusResource = useEngineeringResource(
    statusLoader,
    mergeTrainStatusScopeKey(
      selectedTarget?.key ?? "",
      policyResource.state.lastSuccessfulAt,
      policyResource.state.data?.policy.policy_sha256 ?? "",
      fixtureMode,
    ),
    Boolean(selectedTarget),
  );

  function chooseTarget(key: string) {
    setTargetSelection({ invalid: false, key });
    const target = targets.find((candidate) => candidate.key === key);
    if (!target) {
      return;
    }
    writeTargetToLocation(target);
  }

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={policyResource.cancel}
          refresh={policyResource.refresh}
          refreshLabel="Refresh targets"
          state={policyResource.state}
        />
      }
      description="Inspect DB-backed policy targets, controller admission, durable records, lease state, and reconciliation evidence without advancing the train."
      icon={GitMerge}
      title="Merge train"
      view="merge-train"
    >
      <EngineeringBoundaryNote title="Status-only browser boundary">
        {MERGE_TRAIN_BROWSER_BOUNDARY} The controller and legacy worker POST
        routes remain outside the generated browser write allowlist.
      </EngineeringBoundaryNote>
      <EngineeringResourceGate
        noun="merge-train policy targets"
        refresh={policyResource.refresh}
        state={policyResource.state}
      >
        {(policyData) => (
          <MergeTrainContent
            chooseTarget={chooseTarget}
            invalidTargetQuery={targetSelection.invalid}
            policyData={policyData}
            requestedTargetKey={selectedKey}
            selectedTarget={selectedTarget}
            statusResource={statusResource}
            targets={targets}
          />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function MergeTrainContent({
  chooseTarget,
  invalidTargetQuery,
  policyData,
  requestedTargetKey,
  selectedTarget,
  statusResource,
  targets,
}: {
  chooseTarget: (key: string) => void;
  invalidTargetQuery: boolean;
  policyData: MergeTrainPolicyTargetsResponse;
  requestedTargetKey: string;
  selectedTarget: MergeTrainTargetOption | null;
  statusResource: EngineeringResourceController<MergeTrainControllerStatusResponse>;
  targets: MergeTrainTargetOption[];
}) {
  if (!targets.length) {
    return (
      <EngineeringEmpty
        detail="The active DB-backed merge-train policy contains no authorized repository/base-branch targets."
        icon={Route}
        title="No merge-train targets"
      />
    );
  }

  return (
    <div className="engineering-merge-train">
      <section className="engineering-merge-targets">
        <label>
          <span>Policy target</span>
          <select
            value={selectedTarget?.key ?? ""}
            onChange={(event) => chooseTarget(event.target.value)}
          >
            <option value="" disabled>
              Select a policy target
            </option>
            {targets.map((target) => (
              <option key={target.key} value={target.key}>
                {target.label}
              </option>
            ))}
          </select>
        </label>
        <div>
          <span>Policy record</span>
          <strong>{policyData.policy.record_id}</strong>
          <code>{policyData.policy.policy_sha256}</code>
        </div>
        <div>
          <span>Policy updated</span>
          <strong>{formatTime(policyData.policy.updated_at)}</strong>
          {policyData.trace_id ? <code>{policyData.trace_id}</code> : null}
        </div>
      </section>

      {selectedTarget ? (
        <section className="engineering-policy-card">
          <div>
            <span>Repository</span>
            <strong>{selectedTarget.repository}</strong>
          </div>
          <div>
            <span>Base branch</span>
            <strong>{selectedTarget.baseBranch}</strong>
          </div>
          <div>
            <span>Scheduler</span>
            <strong>
              {selectedTarget.target.scheduler.enabled ? "Enabled" : "Disabled"}
            </strong>
            <small>
              {selectedTarget.target.scheduler.runner_mode} · mutate
              {selectedTarget.target.scheduler.mutate ? " enabled" : " disabled"}
            </small>
          </div>
          <div>
            <span>Service authorization</span>
            <strong>{selectedTarget.target.service_authz.action}</strong>
            <small>
              {selectedTarget.target.service_authz.product} / {selectedTarget.target.service_authz.context}
            </small>
          </div>
        </section>
      ) : null}

      {selectedTarget ? (
        <>
          <div className="engineering-status-toolbar">
            <div>
              <span>Controller evidence</span>
              <strong>{selectedTarget.label}</strong>
            </div>
            <EngineeringResourceControls
              cancel={statusResource.cancel}
              refresh={statusResource.refresh}
              refreshLabel="Refresh status"
              state={statusResource.state}
            />
          </div>
          <EngineeringResourceGate
            noun="merge-train controller status"
            refresh={statusResource.refresh}
            state={statusResource.state}
          >
            {(data) => (
              <ControllerStatus
                data={data}
                expectedPolicySha256={policyData.policy.policy_sha256}
              />
            )}
          </EngineeringResourceGate>
        </>
      ) : (
        <EngineeringEmpty
          detail={
            requestedTargetKey || invalidTargetQuery
              ? "The requested repository/base-branch pair is not present in the active authorized policy. No controller status request was sent."
              : "Choose one authorized DB-backed policy target before loading controller evidence."
          }
          icon={requestedTargetKey || invalidTargetQuery ? ShieldAlert : GitMerge}
          title={
            requestedTargetKey || invalidTargetQuery
              ? "Target unavailable"
              : "Select a merge-train target"
          }
        />
      )}
    </div>
  );
}

function ControllerStatus({
  data,
  expectedPolicySha256,
}: {
  data: MergeTrainControllerStatusResponse;
  expectedPolicySha256: string;
}) {
  const status = data.controller_status;
  const controllerTone = mergeTrainControllerTone(status, expectedPolicySha256);
  const policyDigestMismatch =
    Boolean(expectedPolicySha256) &&
    status.current_policy_sha256 !== expectedPolicySha256;
  const reconciliationRequired =
    status.controller_state?.status === "reconcile_required" ||
    status.controller_diagnostics?.reconciliation_status === "required";
  const currentRecords = status.controller_records.filter(
    (record) => record.policy_status === "current",
  );
  const staleRecords = status.controller_records.filter(
    (record) => record.policy_status === "stale",
  );
  return (
    <div className="engineering-controller-status">
      <section className="engineering-metric-grid" aria-label="Controller summary">
        <Metric label="Admission" value={status.admission.status} />
        <Metric
          label="Controller"
          value={status.controller_state?.status ?? "No state"}
          tone={controllerTone}
        />
        <Metric label="Current records" value={String(currentRecords.length)} />
        <Metric
          label="Stale records"
          value={String(staleRecords.length)}
          tone={staleRecords.length ? "blocked" : "pass"}
        />
        <Metric
          label="Lease"
          value={status.controller_diagnostics?.status || "No lease"}
          tone={status.controller_diagnostics ? "pending" : "unknown"}
        />
      </section>

      {policyDigestMismatch || reconciliationRequired || staleRecords.length ? (
        <div className="engineering-controller-blocker" role="alert">
          <ShieldAlert size={18} aria-hidden="true" />
          <div>
            <strong>Controller evidence requires attention</strong>
            <p>
              {policyDigestMismatch
                ? "The controller status policy digest does not match the selected active policy."
                : reconciliationRequired
                  ? status.controller_state?.reconciliation_detail ||
                    status.controller_diagnostics?.reconciliation_detail ||
                    "Controller reconciliation is required."
                  : `${staleRecords.length} durable record${staleRecords.length === 1 ? " is" : "s are"} stale under the active policy.`}
            </p>
          </div>
        </div>
      ) : null}

      <section className="engineering-controller-summary">
        <div>
          <span className="engineering-kicker">Current controller action</span>
          <h2>{humanize(status.admission.controller_action || "idle")}</h2>
          <p>
            {status.admission.detail ||
              status.admission.controller_reason ||
              "No controller detail recorded."}
          </p>
        </div>
        <span
          className="engineering-status-chip"
          data-status={controllerTone}
        >
          <StatusIcon status={controllerTone} />
          {status.controller_state?.reconciliation_status ?? "no state"}
        </span>
      </section>

      <section className="engineering-evidence-strip" aria-label="Controller evidence">
        <div>
          <span>Generated</span>
          <strong>{formatTime(status.generated_at)}</strong>
          {data.trace_id ? <code>{data.trace_id}</code> : null}
        </div>
        <div>
          <span>Policy key</span>
          <strong>{status.current_policy_key}</strong>
          <code>{status.current_policy_sha256}</code>
        </div>
        <div>
          <span>Next allowed</span>
          <strong>{formatTime(status.admission.next_allowed_at)}</strong>
          <small>{humanize(status.admission.reason_code)}</small>
        </div>
        <div>
          <span>Lease owner</span>
          <strong>{status.controller_diagnostics?.owner || "No active owner"}</strong>
          <small>
            {formatAge(status.controller_diagnostics?.heartbeat_age_seconds)} heartbeat
          </small>
        </div>
      </section>

      {status.controller_state ? (
        <section className="engineering-controller-state">
          <div>
            <span>Active phase</span>
            <strong>{humanize(status.controller_state.active_phase || "idle")}</strong>
            <small>{status.controller_state.active_record_id || "No active record"}</small>
          </div>
          <div>
            <span>Last transition</span>
            <strong>{humanize(status.controller_state.last_action || "none")}</strong>
            <small>{formatTime(status.controller_state.last_transition_at)}</small>
          </div>
          <div>
            <span>Reconciliation</span>
            <strong>{humanize(status.controller_state.reconciliation_status)}</strong>
            <small>
              {status.controller_state.reconciliation_detail || "No reconciliation detail"}
            </small>
          </div>
        </section>
      ) : null}

      {status.latest_run ? (
        <section className="engineering-latest-run">
          <ListChecks size={19} aria-hidden="true" />
          <div>
            <span className="engineering-kicker">Latest ordered-queue run</span>
            <h2>{humanize(status.latest_run.status)}</h2>
            <p>
              {status.latest_run.selected_pr_number
                ? `Selected PR #${status.latest_run.selected_pr_number}; ${status.latest_run.selected_required_checks_status || "checks unknown"}.`
                : "No pull request was selected."}
            </p>
          </div>
          <code>{status.latest_run.run_id}</code>
          <time dateTime={status.latest_run.recorded_at}>
            {formatTime(status.latest_run.recorded_at)}
          </time>
        </section>
      ) : null}

      {status.latest_dry_run ? (
        <section className="engineering-dry-run">
          <header>
            <div>
              <span className="engineering-kicker">Latest dry-run evidence</span>
              <h2>{humanize(status.latest_dry_run.intended_next_action)}</h2>
              <p>{status.latest_dry_run.next_action_detail}</p>
            </div>
            <div className="engineering-chip-row">
              <span>{status.latest_dry_run.queue_count} queued</span>
              <span>{status.latest_dry_run.eligible_count} eligible</span>
              {status.latest_dry_run.selected_pr_number ? (
                <span>PR #{status.latest_dry_run.selected_pr_number}</span>
              ) : null}
            </div>
          </header>
          {status.latest_dry_run.queue_entries.length ? (
            <ul>
              {status.latest_dry_run.queue_entries.map((entry) => {
                const url = safeExternalUrl(entry.url);
                return (
                  <li key={entry.pull_request_number} data-eligible={entry.eligible}>
                    <StatusIcon status={entry.eligible ? "pass" : "blocked"} />
                    <div>
                      <strong>PR #{entry.pull_request_number} · {entry.title}</strong>
                      <span>
                        {entry.eligible
                          ? `${entry.mergeable || "mergeable"} · ${entry.required_checks_status || "checks unknown"}`
                          : entry.ineligible_reasons.join(", ") || "Not eligible"}
                      </span>
                    </div>
                    {url ? (
                      <a
                        aria-label={`Open PR #${entry.pull_request_number} (opens in a new tab)`}
                        href={url.href}
                        rel="noreferrer"
                        target="_blank"
                      >
                        Open
                        <ExternalLink size={13} aria-hidden="true" />
                      </a>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
        </section>
      ) : null}

      <section className="engineering-controller-records">
        <header>
          <div>
            <span className="engineering-kicker">Durable controller evidence</span>
            <h2>Controller records</h2>
          </div>
          <span>{status.controller_records.length} records</span>
        </header>
        {!status.controller_records.length ? (
          <EngineeringEmpty
            detail="No candidate, landing, or stack-collapse records are visible for the active policy."
            icon={Activity}
            title="No controller records"
          />
        ) : (
          <ul>
            {status.controller_records.map((record) => (
              <ControllerRecord key={record.record_id} record={record} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ControllerRecord({ record }: { record: MergeTrainControllerRecordSummary }) {
  const tone = record.policy_status === "stale" ? "blocked" : "pending";
  return (
    <li className="engineering-controller-record" data-policy={record.policy_status}>
      <StatusIcon status={tone} />
      <div>
        <strong>{humanize(record.record_type)}</strong>
        <code>{record.record_id}</code>
      </div>
      <div className="engineering-chip-row">
        <span>{record.status}</span>
        <span>{record.policy_status} policy</span>
        {record.pull_request_numbers.length ? (
          <span>PR {record.pull_request_numbers.join(", ")}</span>
        ) : null}
        {record.required_checks_status ? (
          <span>{record.required_checks_status}</span>
        ) : null}
      </div>
      <p>
        {record.stale_reason ||
          `${record.merged_count}/${record.planned_count} merged · ${record.blocked_count} blocked · ${record.skipped_count} skipped`}
      </p>
      <time dateTime={record.updated_at}>{formatTime(record.updated_at)}</time>
    </li>
  );
}

function Metric({
  label,
  tone = "unknown",
  value,
}: {
  label: string;
  tone?: string;
  value: string;
}) {
  return (
    <div className="engineering-metric" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function writeTargetToLocation(target: MergeTrainTargetOption): void {
  const url = new URL(window.location.href);
  url.searchParams.set("repository", target.repository);
  url.searchParams.set("base_branch", target.baseBranch);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "Unknown";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${Math.round(seconds / 3600)}h`;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}
