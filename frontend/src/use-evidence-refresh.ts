import { useEffect, useRef } from "react";

import type { ResourceState } from "./resource";

const READ_REFRESH_INTERVAL_MS = 60_000;

// Refresh reads independently of the shell's manual refresh, which also resets
// action resources. The existing loaders own cancellation and retained data.
export function useEvidenceRefresh<T>(
  key: string,
  enabled: boolean,
  resource: ResourceState<T>,
  refresh: () => void,
) {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!enabled || !["ready", "error"].includes(resource.status) ||
        [401, 403, 404].includes(resource.statusCode)) return;

    let timer: number;
    let requested = false;
    const dueAt = Date.now() + READ_REFRESH_INTERVAL_MS;
    const update = () => {
      if (requested || document.visibilityState !== "visible" || Date.now() < dueAt) return;
      requested = true;
      window.clearTimeout(timer);
      refreshRef.current();
    };
    timer = window.setTimeout(update, READ_REFRESH_INTERVAL_MS);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [enabled, key, resource.status, resource.statusCode]);
}
