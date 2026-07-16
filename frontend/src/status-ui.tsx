import { AlertTriangle, CheckCircle2, Clock3, XCircle } from "lucide-react";

import type { Status } from "./types";

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
