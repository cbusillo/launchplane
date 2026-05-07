import { useCallback, useState } from "react";

import { readWorkGraphSnapshot, rankWorkGraphSnapshot } from "./api";
import type { WorkGraphQueueItem } from "./types";

export function useWorkGraphQueue() {
  const [items, setItems] = useState<WorkGraphQueueItem[]>([]);
  const [hiddenCount, setHiddenCount] = useState(0);
  const [error, setError] = useState("");

  const reset = useCallback(() => {
    setItems([]);
    setHiddenCount(0);
    setError("");
  }, []);

  const refresh = useCallback(
    async (signal: AbortSignal) => {
      const snapshotPayload = await readWorkGraphSnapshot();
      if (signal.aborted) {
        return;
      }

      const snapshot = snapshotPayload.snapshot;
      if (!snapshot.issues.length) {
        reset();
        return;
      }

      try {
        const workGraphPayload = await rankWorkGraphSnapshot(snapshot, 12);
        if (signal.aborted) {
          return;
        }
        setItems(workGraphPayload.result.queue.items);
        setHiddenCount(workGraphPayload.result.queue.hidden_count);
        setError("");
      } catch (apiError) {
        if (signal.aborted) {
          return;
        }
        setItems([]);
        setHiddenCount(0);
        setError(
          apiError instanceof Error
            ? apiError.message
            : "Work graph ranking failed.",
        );
      }
    },
    [reset],
  );

  return {
    workGraphItems: items,
    workGraphHiddenCount: hiddenCount,
    workGraphError: error,
    refreshWorkGraph: refresh,
    resetWorkGraph: reset,
  };
}
