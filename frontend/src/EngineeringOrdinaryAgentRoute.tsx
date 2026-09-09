import { KeyRound } from "lucide-react";
import { useCallback, useState } from "react";

import {
  approveOrdinaryAgentOperation,
  cancelOrdinaryAgentOperation,
  disconnectOrdinaryAgent,
  readOrdinaryAgentOperation,
  revokeOrdinaryAgentSession,
  type OrdinaryAgentOperationClientResponse,
} from "./api";
import { useEngineeringResource } from "./engineering-resource";
import {
  EngineeringResourceControls,
  EngineeringResourceGate,
  EngineeringRouteFrame,
} from "./EngineeringRouteUi";
import { OrdinaryAgentDelegationReview } from "./OrdinaryAgentDelegationReview";

const permissionLabels = {
  self_read: "Read this agent’s status",
  preflight: "Check merge readiness",
  guarded_merge: "Merge through Launchplane when required checks pass",
};

export function EngineeringOrdinaryAgentRoute({ principalId, operationId }: {
  principalId: string; operationId: string;
}) {
  const loader = useCallback((signal: AbortSignal) => readOrdinaryAgentOperation(principalId, operationId, signal), [principalId, operationId]);
  const resource = useEngineeringResource(loader, `ordinary-agent:${principalId}:${operationId}`);
  return (
    <EngineeringRouteFrame
      title="Agent access request" description="Review what this agent can do and when its access ends."
      icon={KeyRound} view="privileged-operations"
      actions={<EngineeringResourceControls cancel={resource.cancel} refresh={resource.refresh} refreshLabel="Refresh request" state={resource.state} />}
    >
      <EngineeringResourceGate noun="Agent request" refresh={resource.refresh} state={resource.state}>
        {(data) => <OrdinaryAgentOperationCard key={`${principalId}:${operationId}`} data={data} refresh={resource.refresh} />}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function OrdinaryAgentOperationCard({ data, refresh }: {
  data: OrdinaryAgentOperationClientResponse; refresh: () => void;
}) {
  const view = data.operation;
  const bounds = view.attenuation;
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [disconnectEvent] = useState(() => `ui-disconnect-${crypto.randomUUID()}`);
  const requester = [view.requester_subject, view.requester_token_label].filter(Boolean).join(" · ");
  const requestExpiry = view.kind === "initial"
    ? Math.min(view.delivery_expires_at ?? Number.NaN, bounds?.session_expires_at ?? Number.POSITIVE_INFINITY)
    : bounds?.session_expires_at ?? Number.NaN;

  async function control(action: "cancel" | "revoke" | "disconnect") {
    setBusy(true);
    setMessage("");
    try {
      if (action === "cancel") {
        await cancelOrdinaryAgentOperation(view.principal_id, view.operation_id);
        setMessage("Request cancelled.");
      } else if (action === "revoke" && view.session_id) {
        await revokeOrdinaryAgentSession(view.principal_id, view.session_id);
        setMessage("Session revoked. Pending work is cancelled; actions already running may finish.");
      } else if (action === "disconnect") {
        await disconnectOrdinaryAgent(view.principal_id, disconnectEvent);
        setMessage("Agent disconnected. All of its sessions are revoked.");
      }
      refresh();
    } catch {
      setMessage("The change could not be confirmed. Refresh this request to see its current state.");
    } finally {
      setBusy(false);
    }
  }

  return <>
    <OrdinaryAgentDelegationReview
      kind={view.kind} status={view.status} requester={requester}
      repository={view.target.repository} baseBranch={view.target.base_branch}
      currentExecutionProfile={view.current_policy_execution_profile}
      permissions={bounds?.actions.map((action) => permissionLabels[action]) ?? []}
      credentialExpiresAt={view.credential_expires_at}
      requestExpiresAt={requestExpiry}
      sessionExpiresAt={bounds?.session_expires_at ?? null}
      leaseExpiresAt={bounds?.lease_expires_at ?? null}
      actionLimit={bounds?.action_limit ?? null} pullRequestLimit={bounds?.pull_request_limit ?? null}
      refreshAllowance={bounds?.refresh_allowance ?? null}
      continuationExpiresAt={bounds?.continuation_expires_at ?? null}
      canApprove={view.can_approve && !busy} applied={view.applied}
      onApprove={async () => { setBusy(true); try { await approveOrdinaryAgentOperation(view.principal_id, view.operation_id); refresh(); } finally { setBusy(false); } }}
    />
    <section aria-label="Manage this agent request" className="privileged-operation-actions ordinary-agent-controls">
      {!view.applied && (view.status === "pending" || view.status === "approved") ?
        <button className="button" type="button" disabled={busy} onClick={() => void control("cancel")}>Cancel request</button> : null}
      {view.session_id && view.status !== "revoked" ?
        <button className="button" type="button" disabled={busy} onClick={() => void control("revoke")}>Revoke this session</button> : null}
      {view.applied ? <button className="button" type="button" disabled={busy} onClick={() => void control("disconnect")}>Disconnect agent (all sessions)</button> : null}
      {message ? <p role="status">{message}</p> : null}
    </section>
  </>;
}
