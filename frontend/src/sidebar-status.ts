import { laneSignal, type LaneStatusEvidence } from "./product-environment-signal";

export function sidebarStatus(environment: LaneStatusEvidence | null, label: string, staleDescription = "") {
  if (staleDescription) return { tone: "stale" as const, description: `${label}: ${staleDescription}` };
  const signal = laneSignal(environment);
  if (!signal) return { tone: "missing" as const, description: `${label}: ${environment ? "Unknown (status unavailable)" : "Absent (not recorded)"}` };
  return { tone: signal.sidebar_tone, description: `${label}: ${signal.description}` };
}
