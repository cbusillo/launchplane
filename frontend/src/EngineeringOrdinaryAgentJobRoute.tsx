import { ListChecks } from "lucide-react";
import { useCallback } from "react";

import { readOrdinaryAgentJob } from "./api";
import { useEngineeringResource } from "./engineering-resource";
import { EngineeringResourceControls, EngineeringResourceGate, EngineeringRouteFrame } from "./EngineeringRouteUi";

const statusLabels = {
  pending: "Waiting to start", running: "Working", waiting: "Waiting to continue",
  blocked: "Cannot continue", cancelled: "Cancelled", partially_completed: "Partly completed",
  completed: "Completed", reconciliation_required: "Checking an uncertain result",
};

function deadline(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}

export function EngineeringOrdinaryAgentJobRoute({ principalId, requestId }: {
  principalId: string; requestId: string;
}) {
  const loader = useCallback((signal: AbortSignal) => readOrdinaryAgentJob(principalId, requestId, signal), [principalId, requestId]);
  const resource = useEngineeringResource(loader, `ordinary-job:${principalId}:${requestId}`);
  return <EngineeringRouteFrame title="Agent engineering work" description="See the current result of this agent’s requested work."
    icon={ListChecks} view="privileged-operations"
    actions={<EngineeringResourceControls cancel={resource.cancel} refresh={resource.refresh} refreshLabel="Refresh work" state={resource.state} />}>
    <EngineeringResourceGate noun="Agent work" refresh={resource.refresh} state={resource.state}>
      {(job) => <article className="privileged-operation-card">
        <h2>{statusLabels[job.status]}</h2>
        <p>{job.target.repository} · {job.target.base_branch}</p>
        <p>Pull requests: {job.pull_request_numbers.map((number) => `#${number}`).join(", ")}</p>
        <p>{job.completed_effects} completed {job.completed_effects === 1 ? "action" : "actions"} recorded.</p>
        {job.cancellation_requested ? <p>Cancellation requested. An action already sent may still finish.</p> : null}
        {job.unresolved_effects > 0 ? <p role="status">{job.unresolved_effects} {job.unresolved_effects === 1 ? "action result still needs" : "action results still need"} verification. Launchplane will not repeat an uncertain action blindly.</p> : null}
        {job.next_due_at !== null ? <p>Next check: {deadline(job.next_due_at)}</p> : null}
        <p>Work expires: {deadline(job.expires_at)}</p>
        {job.continuation_expires_at !== null ? <p>Already-started work may finish until {deadline(job.continuation_expires_at)}.</p> : null}
      </article>}
    </EngineeringResourceGate>
  </EngineeringRouteFrame>;
}
