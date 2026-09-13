import { AlertTriangle, CheckCircle2, LoaderCircle, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  LaunchplaneApiError,
  prepareOrdinaryAgentMergeTrainTarget,
  readOrdinaryAgentMergeTrainTargetInputs,
  type OrdinaryAgentMergeTrainTargetInputsResponse,
  type OrdinaryAgentMergeTrainTargetIntent,
} from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
} from "./engineering-resource";
import { EngineeringEmpty, EngineeringResourceGate } from "./EngineeringRouteUi";
import { navigateTo } from "./router";

type MergeMethod = OrdinaryAgentMergeTrainTargetIntent["merge_method"];
type ReviewMode = OrdinaryAgentMergeTrainTargetIntent["engineering_review_mode"];
type FailurePolicy = OrdinaryAgentMergeTrainTargetIntent["failure_policy"];
type IdentityKind = OrdinaryAgentMergeTrainTargetIntent["merge_identity"]["kind"];

type FormValues = {
  repositoryId: string;
  baseBranch: string;
  enqueueLabel: string;
  blockedLabel: string;
  stackChildDispositionLabel: string;
  mergeMethod: MergeMethod | "";
  engineeringReviewMode: ReviewMode | "";
  failurePolicy: FailurePolicy | "";
  labelRequired: "" | "true" | "false";
  allowedActorRoles: Array<"repo_owner" | "repo_admin">;
  trustedAutomationIds: string;
  identityKind: IdentityKind | "";
  identityName: string;
};

const INITIAL_FORM: FormValues = {
  repositoryId: "",
  baseBranch: "",
  enqueueLabel: "",
  blockedLabel: "",
  stackChildDispositionLabel: "",
  mergeMethod: "",
  engineeringReviewMode: "",
  failurePolicy: "",
  labelRequired: "",
  allowedActorRoles: [],
  trustedAutomationIds: "",
  identityKind: "",
  identityName: "",
};

export function EngineeringOrdinaryAgentTargetPreparation({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<OrdinaryAgentMergeTrainTargetInputsResponse> => {
      const preparationMode = new URLSearchParams(window.location.search).get(
        "preparation",
      );
      if (fixtureMode && preparationMode !== "api") {
        const fixtures = await loadDevFixtures();
        return fixtures.mergeTrainTargetInputsForFixture(fixtureMode);
      }
      return readOrdinaryAgentMergeTrainTargetInputs(signal);
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `ordinary-merge-target-inputs:${fixtureMode}`,
  );

  return (
    <section
      className="ordinary-target-preparation-card"
      aria-labelledby="ordinary-target-preparation-title"
    >
      <header>
        <div>
          <span className="engineering-kicker">One-time engineering setup</span>
          <h2 id="ordinary-target-preparation-title">
            Prepare ordinary-agent delivery target
          </h2>
          <p>
            Choose one tracked repository and its merge-train settings. This
            prepares an inert plan for the existing human review flow.
          </p>
        </div>
      </header>
      <div className="ordinary-target-preparation-boundary" role="note">
        <ShieldAlert size={17} aria-hidden="true" />
        <span>
          The new target is ordinary-agent only: scheduler and mutation stay
          off, and no ambient token is attached. Merge identity is metadata for
          this policy and does not prove provider custody. Service authorization
          remains Launchplane's shared control-plane gate; this setup does not
          claim controller custody or activate delivery.
        </span>
      </div>
      <EngineeringResourceGate
        noun="ordinary-agent target inputs"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) =>
          data.tracked_repositories.length ? (
            <TargetPreparationForm data={data} />
          ) : (
            <EngineeringEmpty
              detail="Launchplane has no current tracked repository identity to use for a new target."
              icon={AlertTriangle}
              title="No tracked repositories available"
            />
          )
        }
      </EngineeringResourceGate>
    </section>
  );
}

function TargetPreparationForm({
  data,
}: {
  data: OrdinaryAgentMergeTrainTargetInputsResponse;
}) {
  const [values, setValues] = useState<FormValues>(INITIAL_FORM);
  const [message, setMessage] = useState("");
  const [traceId, setTraceId] = useState("");
  const [pending, setPending] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const retryRef = useRef<{ fingerprint: string; sourceEventId: string } | null>(null);
  const enqueueLabel = values.enqueueLabel.trim();
  const blockedLabel = values.blockedLabel.trim();
  const stackLabel = values.stackChildDispositionLabel.trim();
  const validationMessage =
    enqueueLabel && blockedLabel && enqueueLabel === blockedLabel
      ? "Use different labels for enqueued and blocked pull requests."
      : stackLabel && (stackLabel === enqueueLabel || stackLabel === blockedLabel)
        ? "Use a separate label for completed stack children."
        : parseTrustedAutomationIds(values.trustedAutomationIds) === null
          ? "Enter positive whole-number automation IDs separated by commas, or leave this optional field empty."
          : "";

  useEffect(
    () => () => controllerRef.current?.abort(),
    [],
  );

  const update = <K extends keyof FormValues>(key: K, value: FormValues[K]) => {
    setValues((current) => ({ ...current, [key]: value }));
    setMessage("");
    setTraceId("");
  };

  function toggleRole(role: "repo_owner" | "repo_admin") {
    update(
      "allowedActorRoles",
      values.allowedActorRoles.includes(role)
        ? values.allowedActorRoles.filter((candidate) => candidate !== role)
        : [...values.allowedActorRoles, role],
    );
  }

  function buildIntent(): OrdinaryAgentMergeTrainTargetIntent | null {
    if (
      validationMessage ||
      !values.repositoryId ||
      !values.baseBranch.trim() ||
      !values.enqueueLabel.trim() ||
      !values.blockedLabel.trim() ||
      !values.mergeMethod ||
      !values.engineeringReviewMode ||
      !values.failurePolicy ||
      values.labelRequired === "" ||
      !values.allowedActorRoles.length ||
      !values.identityKind ||
      !values.identityName.trim()
    ) {
      return null;
    }
    const trustedAutomationIds = parseTrustedAutomationIds(values.trustedAutomationIds);
    if (trustedAutomationIds === null) return null;
    return {
      repository_id: values.repositoryId,
      base_branch: values.baseBranch.trim(),
      enqueue_label: values.enqueueLabel.trim(),
      blocked_label: values.blockedLabel.trim(),
      stack_child_disposition_label: values.stackChildDispositionLabel.trim(),
      merge_method: values.mergeMethod,
      engineering_review_mode: values.engineeringReviewMode,
      failure_policy: values.failurePolicy,
      enqueue: {
        label_required: values.labelRequired === "true",
        allowed_actor_roles: values.allowedActorRoles,
        ...(trustedAutomationIds.length
          ? { trusted_automation_github_user_ids: trustedAutomationIds }
          : {}),
      },
      merge_identity: {
        kind: values.identityKind,
        name: values.identityName.trim(),
      },
    };
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const intent = buildIntent();
    if (!intent) {
      setMessage("Complete each required engineering choice before preparing the plan.");
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setPending(true);
    setMessage("");
    setTraceId("");
    try {
      const fingerprint = JSON.stringify(intent);
      const sourceEventId =
        retryRef.current?.fingerprint === fingerprint
          ? retryRef.current.sourceEventId
          : buildSourceEventId();
      retryRef.current = { fingerprint, sourceEventId };
      const response = await prepareOrdinaryAgentMergeTrainTarget(
        intent,
        sourceEventId,
        controller.signal,
      );
      setTraceId(response.trace_id);
      if (response.state === "already_satisfied") {
        setMessage("This repository and branch already have the requested target.");
        retryRef.current = null;
        return;
      }
      if (!response.operation_id) {
        setMessage("The plan was prepared, but its review is not available yet.");
        return;
      }
      retryRef.current = null;
      navigateTo(
        `/ui/engineering/privileged-operations?operation_id=${encodeURIComponent(response.operation_id)}`,
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      setMessage(
        error instanceof LaunchplaneApiError
          ? error.message
          : "Ordinary-agent target preparation is unavailable.",
      );
      if (error instanceof LaunchplaneApiError) setTraceId(error.traceId);
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setPending(false);
      }
    }
  }

  const submitDisabled = pending || !buildIntent();
  return (
    <form className="ordinary-target-preparation-form" onSubmit={submit}>
      <fieldset disabled={pending}>
        <legend>Target intent</legend>
        <label>
          Repository
          <select
            aria-label="Repository"
            value={values.repositoryId}
            onChange={(event) => update("repositoryId", event.target.value)}
          >
            <option value="">Choose a tracked repository</option>
            {data.tracked_repositories.map((repository) => (
              <option key={repository.repository_id} value={repository.repository_id}>
                {repository.repository}
              </option>
            ))}
          </select>
        </label>
        <label>
          Base branch
          <input
            aria-label="Base branch"
            value={values.baseBranch}
            onChange={(event) => update("baseBranch", event.target.value)}
            placeholder="main"
          />
        </label>
        <label>
          Enqueue label
          <input
            aria-label="Enqueue label"
            value={values.enqueueLabel}
            onChange={(event) => update("enqueueLabel", event.target.value)}
            placeholder="merge-train"
          />
        </label>
        <label>
          Blocked label
          <input
            aria-label="Blocked label"
            value={values.blockedLabel}
            onChange={(event) => update("blockedLabel", event.target.value)}
            placeholder="merge-train-blocked"
          />
        </label>
        <label>
          Stack child disposition label <span className="field-optional">(optional)</span>
          <input
            aria-label="Stack child disposition label (optional)"
            value={values.stackChildDispositionLabel}
            onChange={(event) => update("stackChildDispositionLabel", event.target.value)}
            placeholder="Leave empty when stack disposition is not used"
          />
        </label>
        <label>
          Merge method
          <select
            aria-label="Merge method"
            value={values.mergeMethod}
            onChange={(event) => update("mergeMethod", event.target.value as MergeMethod)}
          >
            <option value="">Choose a merge method</option>
            <option value="merge">Merge commit</option>
            <option value="squash">Squash</option>
            <option value="rebase">Rebase</option>
          </select>
        </label>
        <label>
          Engineering review
          <select
            aria-label="Engineering review"
            value={values.engineeringReviewMode}
            onChange={(event) => update("engineeringReviewMode", event.target.value as ReviewMode)}
          >
            <option value="">Choose review evidence</option>
            <option value="advisory">Advisory</option>
            <option value="required">Required</option>
          </select>
        </label>
        <label>
          Failure handling
          <select
            aria-label="Failure handling"
            value={values.failurePolicy}
            onChange={(event) => update("failurePolicy", event.target.value as FailurePolicy)}
          >
            <option value="">Choose failure handling</option>
            <option value="pause_train">Pause the train</option>
            <option value="continue_after_blocking_pr">Continue after a blocking pull request</option>
          </select>
        </label>
      </fieldset>

      <fieldset disabled={pending}>
        <legend>Enqueue authority</legend>
        <label>
          Require the enqueue label
          <select
            aria-label="Require the enqueue label"
            value={values.labelRequired}
            onChange={(event) => update("labelRequired", event.target.value as FormValues["labelRequired"])}
          >
            <option value="">Choose label requirement</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </label>
        <div className="ordinary-target-checkboxes" aria-label="Allowed actor roles">
          <span>Allowed actor roles</span>
          {(["repo_owner", "repo_admin"] as const).map((role) => (
            <label key={role}>
              <input
                type="checkbox"
                checked={values.allowedActorRoles.includes(role)}
                onChange={() => toggleRole(role)}
              />
              {role === "repo_owner" ? "Repository owner" : "Repository admin"}
            </label>
          ))}
        </div>
        <label>
          Trusted automation GitHub IDs <span className="field-optional">(optional)</span>
          <input
            aria-label="Trusted automation GitHub IDs (optional)"
            value={values.trustedAutomationIds}
            onChange={(event) => update("trustedAutomationIds", event.target.value)}
            placeholder="Comma-separated immutable IDs"
          />
        </label>
      </fieldset>

      <fieldset disabled={pending}>
        <legend>Merge identity metadata</legend>
        <p className="ordinary-target-help">
          This describes the intended merge identity. It is not evidence that
          Launchplane has custody of that provider identity.
        </p>
        <label>
          Identity kind
          <select
            aria-label="Identity kind"
            value={values.identityKind}
            onChange={(event) => update("identityKind", event.target.value as IdentityKind)}
          >
            <option value="">Choose identity metadata</option>
            <option value="github_actions_oidc">GitHub Actions OIDC</option>
            <option value="github_app">GitHub App</option>
            <option value="github_token_secret">GitHub token secret</option>
          </select>
        </label>
        <label>
          Identity name
          <input
            aria-label="Identity name"
            value={values.identityName}
            onChange={(event) => update("identityName", event.target.value)}
            placeholder="Explicit provider identity label"
          />
        </label>
      </fieldset>

      <div className="ordinary-target-unqualified" role="note">
        No provider protection expectation is recorded in this setup. Custody
        and protection remain unqualified until separately evidenced.
      </div>

      {validationMessage ? (
        <p className="ordinary-target-preparation-message" role="alert">
          {validationMessage}
        </p>
      ) : null}

      <div className="ordinary-target-preparation-submit">
        <button type="submit" disabled={submitDisabled}>
          {pending ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <CheckCircle2 size={16} aria-hidden="true" />}
          {pending ? "Preparing plan…" : "Prepare target for review"}
        </button>
        <span>Only this new target intent is sent; Launchplane derives the plan record.</span>
      </div>
      {message ? (
        <p className="ordinary-target-preparation-message" role="alert">
          {message}
          {traceId ? <code>Trace: {traceId}</code> : null}
        </p>
      ) : null}
    </form>
  );
}

function parseTrustedAutomationIds(value: string): number[] | null {
  if (!value.trim()) return [];
  const values = value.split(",").map((candidate) => candidate.trim());
  const ids = values.map((candidate) => Number(candidate));
  if (ids.some((id) => !Number.isSafeInteger(id) || id <= 0)) return null;
  return [...new Set(ids)];
}

function buildSourceEventId(): string {
  const randomId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}`;
  return `ui:ordinary-merge-target:${randomId}`;
}
