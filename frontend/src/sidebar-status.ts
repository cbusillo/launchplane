import type { ProductEnvironmentSummary } from "./generated/openapi.ts";
import { laneSignal } from "./product-environment-signal";

export function sidebarStatus(environment: ProductEnvironmentSummary | null, label: string, staleDescription = "") {
  if (staleDescription) return { tone: "stale" as const, description: `${label}: ${staleDescription}` };
  const signal = laneSignal(environment);
  if (!signal) return { tone: "missing" as const, description: `${label}: ${environment ? "Unknown (status unavailable)" : "Absent (not recorded)"}` };
  return { tone: signal.sidebar_tone, description: `${label}: ${signal.description}` };
}
