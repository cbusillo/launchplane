import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  LaunchplaneApiError,
  prepareOrdinaryAgentDeliveryPolicy,
  type OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
  type OrdinaryAgentDeliveryPolicyIntent,
} from "./api";
import { navigateTo } from "./router";

const DRAFT_KEY = "launchplane:ordinary-agent-policy-preparation:v1";

type PolicyDraft = {
  intent: OrdinaryAgentDeliveryPolicyIntent;
  repositoryLabel: string;
  sourceEventId: string;
};

function readDraft(): PolicyDraft | null {
  try {
    const value = window.sessionStorage.getItem(DRAFT_KEY);
    if (!value) return null;
    const draft = JSON.parse(value) as PolicyDraft;
    if (
      typeof draft.sourceEventId !== "string" ||
      !draft.sourceEventId.startsWith("ui:ordinary-policy:") ||
      typeof draft.repositoryLabel !== "string" ||
      typeof draft.intent?.repository_id !== "string" ||
      typeof draft.intent.base_branch !== "string" ||
      typeof draft.intent.client_label !== "string" ||
      typeof draft.intent.principal_id !== "string" ||
      !/^agent_[a-f0-9]{32}$/.test(draft.intent.principal_id)
    ) {
      return null;
    }
    return draft;
  } catch {
    return null;
  }
}

export function EngineeringOrdinaryAgentPolicyPreparation({
  data,
}: {
  data: OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse;
}) {
  const [draft, setDraft] = useState<PolicyDraft | null>(readDraft);
  const [repositoryId, setRepositoryId] = useState(
    draft?.intent.repository_id ?? "",
  );
  const [baseBranch, setBaseBranch] = useState(draft?.intent.base_branch ?? "");
  const [clientLabel, setClientLabel] = useState(draft?.intent.client_label ?? "");
  const [pending, setPending] = useState(false);
  const [finished, setFinished] = useState(false);
  const [message, setMessage] = useState("");
  const [traceId, setTraceId] = useState("");
  const controllerRef = useRef<AbortController | null>(null);
  const repositories = data.repositories.filter(
    (item) => item.configured_branches.length,
  );
  const selectedRepository = repositories.find(
    (item) => item.repository_id === repositoryId,
  );
  const currentInputsAvailable =
    data.inventory_state === "complete" &&
    data.merge_policy_state === "available";

  useEffect(() => () => controllerRef.current?.abort(), []);

  function discardDraft() {
    if (pending || controllerRef.current) return;
    try {
      window.sessionStorage.removeItem(DRAFT_KEY);
    } catch {
      setMessage("This browser could not discard the saved setup. Retry when browser storage is available.");
      return;
    }
    setDraft(null);
    setTraceId("");
    setMessage("Saved setup discarded. Any plan already recorded stays available in Launchplane for review.");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || controllerRef.current) return;
    if (
      !draft &&
      (!currentInputsAvailable || !selectedRepository || !baseBranch || !clientLabel.trim())
    ) return;
    const nextDraft = draft ?? {
      sourceEventId: `ui:ordinary-policy:${crypto.randomUUID()}`,
      repositoryLabel: selectedRepository!.repository,
      intent: {
        repository_id: repositoryId,
        base_branch: baseBranch,
        principal_id: `agent_${crypto.randomUUID().replaceAll("-", "")}`,
        client_label: clientLabel.trim(),
      },
    };
    const controller = new AbortController();
    controllerRef.current = controller;
    setPending(true);
    setMessage("");
    setTraceId("");
    let saved = false;
    try {
      // This contains setup metadata only. Save before dispatch so a reload can
      // retry the same client and intent without issuing another proposal.
      window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify(nextDraft));
      saved = true;
      setDraft(nextDraft);
      const response = await prepareOrdinaryAgentDeliveryPolicy(
        nextDraft.intent,
        nextDraft.sourceEventId,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (response.state === "planned" && !response.operation_id) {
        throw new Error("The prepared access plan has no reference.");
      }
      try {
        window.sessionStorage.removeItem(DRAFT_KEY);
      } catch {
        // A local cleanup failure cannot change the service's known result.
      }
      if (response.state === "planned" && response.operation_id) {
        navigateTo(
          `/ui/engineering/privileged-operations?descriptor_id=ordinary-agent-delivery-activation&policy_operation_id=${encodeURIComponent(response.operation_id)}`,
        );
        return;
      }
      setFinished(true);
      setMessage(
        "This client access is already configured. Review its delivery setup before connecting the client.",
      );
      setTraceId(response.trace_id);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof LaunchplaneApiError) {
        setMessage(error.message);
        setTraceId(error.traceId);
      } else {
        setMessage(
          saved
            ? "Launchplane could not confirm the setup request. Retry to recover the same plan."
            : "This browser could not save the setup request for recovery. No request was sent.",
        );
      }
    } finally {
      if (!controller.signal.aborted) setPending(false);
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }

  return (
    <section
      className="ordinary-target-preparation-card"
      aria-labelledby="ordinary-policy-preparation-title"
    >
      <header>
        <div>
          <span className="engineering-kicker">New agent client</span>
          <h3 id="ordinary-policy-preparation-title">Prepare client access</h3>
          <p>
            Name the client and choose the project it may deliver to.
            Launchplane prepares the access change for review.
          </p>
        </div>
      </header>
      <p>
        This prepares access only. Delivery starts after the separate setup and
        verification steps.
      </p>
      {draft && !finished ? (
        <p role="status">
          Retry the saved request for {draft.intent.client_label} on{" "}
          {draft.repositoryLabel} · {draft.intent.base_branch} to recover its plan.
        </p>
      ) : null}
      {!draft && (!currentInputsAvailable || !repositories.length) ? (
        <p role="status">
          A project with a recorded delivery branch is needed before preparing
          client access.
        </p>
      ) : (
        <form
          className="ordinary-target-preparation-form"
          onSubmit={(event) => void submit(event)}
        >
          <fieldset disabled={pending || draft !== null || finished}>
            <legend>Client and project</legend>
            <label>
              Client name
              <input
                maxLength={120}
                required
                value={clientLabel}
                onChange={(event) => setClientLabel(event.target.value)}
                placeholder="For example, my CLI agent"
              />
            </label>
            <label>
              Project
              <select
                required
                value={repositoryId}
                onChange={(event) => {
                  setRepositoryId(event.target.value);
                  const repository = repositories.find(
                    (item) => item.repository_id === event.target.value,
                  );
                  setBaseBranch(
                    repository?.configured_branches.length === 1
                      ? repository.configured_branches[0]
                      : "",
                  );
                }}
              >
                <option value="">Choose a project</option>
                {draft && !selectedRepository ? (
                  <option value={repositoryId}>{draft.repositoryLabel}</option>
                ) : null}
                {repositories.map((repository) => (
                  <option key={repository.repository_id} value={repository.repository_id}>
                    {repository.repository}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Delivery branch
              <select
                required
                value={baseBranch}
                onChange={(event) => setBaseBranch(event.target.value)}
              >
                <option value="">Choose a branch</option>
                {draft && !selectedRepository?.configured_branches.includes(baseBranch) ? (
                  <option value={baseBranch}>{baseBranch}</option>
                ) : null}
                {selectedRepository?.configured_branches.map((branch) => (
                  <option key={branch} value={branch}>{branch}</option>
                ))}
              </select>
            </label>
          </fieldset>
          {message ? (
            <div className="ordinary-target-preparation-message" role="status">
              <span>{message}</span>
              {traceId ? <code>{traceId}</code> : null}
            </div>
          ) : null}
          <div className="ordinary-target-preparation-submit">
            <button
              className="button"
              type="submit"
              disabled={
                pending || finished ||
                (!draft && (!repositoryId || !baseBranch || !clientLabel.trim()))
              }
            >
              {pending ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : null}
              {pending
                ? "Preparing access…"
                : draft
                  ? "Retry saved setup"
                  : "Prepare client access for review"}
            </button>
            {draft && !finished ? (
              <button
                className="button secondary"
                type="button"
                disabled={pending}
                onClick={discardDraft}
              >
                Discard saved setup
              </button>
            ) : null}
          </div>
          {draft && !finished ? (
            <p>Discarding the saved setup lets you start again. It does not cancel a plan already recorded in Launchplane.</p>
          ) : null}
        </form>
      )}
    </section>
  );
}
