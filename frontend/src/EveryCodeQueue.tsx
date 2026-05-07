import { TerminalSquare } from "lucide-react";

import { SkeletonRows, StateBlock, StatusIcon } from "./status-ui";
import type { EveryCodeWorkRequestRecord, Status } from "./types";

export function EveryCodeQueue({
  requests,
  loading,
}: {
  requests: EveryCodeWorkRequestRecord[];
  loading: boolean;
}) {
  return (
    <div className="every-code-queue">
      <div className="queue-head">
        <span>Every Code</span>
        <strong>{requests.length ? `${requests.length} active` : "clear"}</strong>
      </div>
      {loading && !requests.length ? <SkeletonRows /> : null}
      {!loading && !requests.length ? (
        <StateBlock icon={<TerminalSquare size={18} />} title="No queued local work" />
      ) : null}
      {requests.slice(0, 4).map((request) => (
        <a
          className="queue-row"
          href={request.issue_url}
          key={request.request_id}
          target="_blank"
          rel="noreferrer"
        >
          <span>
            <strong>{request.issue_title || `Issue #${request.issue_number}`}</strong>
            <code>
              {request.repository}#{request.issue_number}
            </code>
          </span>
          <span
            className="status-pill"
            data-status={workRequestStatus(request.state)}
          >
            <StatusIcon status={workRequestStatus(request.state)} />
            {request.state}
          </span>
        </a>
      ))}
    </div>
  );
}

function workRequestStatus(state: EveryCodeWorkRequestRecord["state"]): Status | string {
  if (state === "done") {
    return "pass";
  }
  if (state === "blocked") {
    return "blocked";
  }
  if (state === "queued" || state === "claimed" || state === "running") {
    return "pending";
  }
  return "unknown";
}
