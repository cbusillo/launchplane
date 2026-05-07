import { AlertTriangle, CheckCircle2, Clock3, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import { labelForStatus } from "./format";
import type { Status } from "./types";

export function StatusPill({ status }: { status: Status | string }) {
  return (
    <span className="status-pill" data-status={status}>
      <StatusIcon status={status} />
      {labelForStatus(status)}
    </span>
  );
}

export function StatusIcon({ status }: { status: Status | string }) {
  if (status === "pass" || status === "ready") {
    return <CheckCircle2 size={15} aria-hidden="true" />;
  }
  if (status === "fail" || status === "blocked") {
    return <XCircle size={15} aria-hidden="true" />;
  }
  if (status === "pending") {
    return <Clock3 size={15} aria-hidden="true" />;
  }
  return <AlertTriangle size={15} aria-hidden="true" />;
}

export function StateBlock({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div className="state-block">
      {icon}
      <strong>{title}</strong>
    </div>
  );
}

export function SkeletonRows() {
  return (
    <div className="skeleton-list" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </div>
  );
}
