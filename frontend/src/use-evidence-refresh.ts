import { useEffect, useRef } from "react";

import type { ResourceState } from "./resource";

export const READ_REFRESH_INTERVAL_MS = 60_000;
export const DEADLINE_RETRY_INTERVAL_MS = 5_000;

// Refresh reads independently of the shell's manual refresh, which also resets
// action resources. The existing loaders own cancellation and retained data.
export function useEvidenceRefresh<T>(
  key: string,
  enabled: boolean,
  resource: ResourceState<T>,
  refresh: () => void,
  evidenceDeadlines: readonly string[] = [],
) {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const deadlineRead = useRef<{ key: string; deadline: number; retryUntil: number } | null>(null);
  // Keep the dependency stable across expiry-only renders. Already expired
  // evidence cannot schedule a read loop; normal polling still retries it.
  const deadlineKey = evidenceDeadlines.join(",");

  useEffect(() => {
    if (!enabled || !["ready", "error"].includes(resource.status) ||
        [401, 403, 404].includes(resource.statusCode)) return;

    let timer: number;
    let requested = false;
    const now = Date.now();
    const deadlines = deadlineKey.split(",").map(value => Date.parse(value)).filter(Number.isFinite);
    const previous = deadlineRead.current;
    const retryDeadline = resource.status === "ready" && previous?.key === key &&
      deadlines.includes(previous.deadline) && now >= previous.deadline &&
      now < previous.deadline + READ_REFRESH_INTERVAL_MS &&
      performance.now() < previous.retryUntil;
    if (!retryDeadline) deadlineRead.current = null;
    const nextExpiry = Math.min(...deadlines.filter(value => value > now));
    const interval = Math.min(READ_REFRESH_INTERVAL_MS, nextExpiry - now,
      retryDeadline ? DEADLINE_RETRY_INTERVAL_MS : Infinity);
    const dueAt = performance.now() + interval;
    const wallDueAt = now + interval;
    const update = () => {
      if (requested || document.visibilityState !== "visible") return;
      // Some browsers pause their monotonic clock during system sleep. A
      // forward wall-clock jump catches up on wake; a backward one cannot
      // postpone the monotonic deadline.
      const remaining = Math.min(dueAt - performance.now(), wallDueAt - Date.now());
      if (remaining > 0) {
        window.clearTimeout(timer);
        timer = window.setTimeout(update, Math.max(1, remaining));
        return;
      }
      requested = true;
      window.clearTimeout(timer);
      // A monitor publishes after probing, so a deadline read may still return
      // its previous observation. Retry that exact deadline for at most one
      // cadence, without renewing proof or creating an unbounded read loop.
      if (!retryDeadline && nextExpiry === wallDueAt) {
        deadlineRead.current = { key, deadline: nextExpiry,
          retryUntil: performance.now() + READ_REFRESH_INTERVAL_MS };
      }
      refreshRef.current();
    };
    timer = window.setTimeout(update, interval);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [enabled, key, resource, deadlineKey]);
}
