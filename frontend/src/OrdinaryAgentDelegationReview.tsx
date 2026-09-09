import { useState } from "react";

export interface OrdinaryAgentDelegationReviewProps {
  kind: "initial" | "existing";
  status: "pending" | "approved" | "expired" | "revoked" | "blocked" | "cancelled";
  requester: string;
  repository: string;
  baseBranch: string;
  permissions: readonly string[];
  credentialExpiresAt: number;
  sessionExpiresAt: number | null;
  leaseExpiresAt: number | null;
  requestExpiresAt: number;
  actionLimit: number | null;
  pullRequestLimit: number | null;
  refreshAllowance: number | null;
  continuationExpiresAt: number | null;
  canApprove: boolean;
  applied: boolean;
  onApprove: () => Promise<void>;
}

function expiryLabel(epoch: number): string {
  const date = new Date(epoch * 1_000);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat(undefined, {
        year: "numeric", month: "short", day: "2-digit",
        hour: "2-digit", minute: "2-digit", timeZoneName: "short",
      }).format(date)
    : "Expiration unavailable";
}

const statusLabels = {
  pending: "Waiting for your approval",
  approved: "Approved",
  expired: "This request has expired",
  revoked: "Access has been revoked",
  blocked: "This request is no longer eligible",
  cancelled: "This request was cancelled",
};

/** Presentation only: all scope and authority values come from the service view. */
export function OrdinaryAgentDelegationReview(
  props: OrdinaryAgentDelegationReviewProps,
) {
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [message, setMessage] = useState("");
  const connection = props.kind === "initial";
  const deadlinesReadable = [props.credentialExpiresAt, props.requestExpiresAt,
    props.sessionExpiresAt, props.leaseExpiresAt, props.continuationExpiresAt].every(
      (epoch) => epoch === null || Number.isFinite(new Date(epoch * 1_000).getTime()),
    );
  const title = connection ? "Connect this agent" : "Allow this engineering session";

  async function approve() {
    setSubmitting(true);
    setMessage("");
    try {
      await props.onApprove();
      setSubmitted(true);
      setMessage("Approval recorded.");
    } catch {
      setMessage("Approval could not be confirmed. Refresh this request before trying again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <article className="privileged-operation-card ordinary-agent-review">
      <header>
        <div>
          <h2>{title}</h2>
          <p>Requested by <strong>{props.requester}</strong></p>
        </div>
        <span role="status">{statusLabels[props.status]}</span>
      </header>
      <div className="ordinary-agent-review-scope">
        <div>
          <h3>{props.repository}</h3>
          <p>Branch: {props.baseBranch}</p>
        </div>
        <div>
          <h3>Requested access</h3>
          {props.permissions.length ? (
            <ul>{props.permissions.map((permission) => <li key={permission}>{permission}</li>)}</ul>
          ) : <p>Connect the agent. Engineering work needs a separately approved session.</p>}
        </div>
      </div>
      <dl className="ordinary-agent-review-limits">
        <div><dt>Approve before</dt><dd>{expiryLabel(props.requestExpiresAt)}</dd></div>
        <div><dt>Connection expires</dt><dd>{expiryLabel(props.credentialExpiresAt)}</dd></div>
        {props.sessionExpiresAt !== null ? (
          <div><dt>Engineering session expires</dt><dd>{expiryLabel(props.sessionExpiresAt)}</dd></div>
        ) : null}
        {props.leaseExpiresAt !== null ? (
          <div><dt>Engineering permission expires</dt><dd>{expiryLabel(props.leaseExpiresAt)}</dd></div>
        ) : null}
        {props.actionLimit !== null ? (
          <div><dt>Action limit</dt><dd>{props.actionLimit}</dd></div>
        ) : null}
        {props.pullRequestLimit !== null ? (
          <div><dt>Pull request limit</dt><dd>{props.pullRequestLimit}</dd></div>
        ) : null}
        {props.refreshAllowance !== null ? (
          <div><dt>Pull request update limit</dt><dd>{props.refreshAllowance}</dd></div>
        ) : null}
        {props.continuationExpiresAt !== null ? (
          <div><dt>Already-approved background work may continue until</dt><dd>{expiryLabel(props.continuationExpiresAt)}</dd></div>
        ) : null}
      </dl>
      {props.permissions.length ? (
        <p>Required engineering checks and owner preview approvals remain in place.</p>
      ) : null}
      {props.status === "approved" ? (
        <p>{props.applied ? (connection ? "Connection prepared." : "Session approved.") : "Launchplane is preparing the approved connection."}</p>
      ) : null}
      {props.canApprove && props.status === "pending" ? (
        <div className="privileged-operation-actions">
          <button type="button" disabled={submitting || submitted || !deadlinesReadable} onClick={() => void approve()}>
            {submitting ? "Recording approval…" : connection ? "Approve connection" : "Approve session"}
          </button>
        </div>
      ) : null}
      {message ? <p role="status">{message}</p> : null}
    </article>
  );
}
