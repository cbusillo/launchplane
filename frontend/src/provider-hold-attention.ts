import type { ProviderHoldAttentionPage } from "./generated/openapi.ts";
import type { ResourceState } from "./resource";

export function providerAttentionQualifier(resource: ResourceState<ProviderHoldAttentionPage>, now = Date.now()): string {
  if (resource.status === "idle" || resource.status === "loading") return "Service operation attention is loading; lane incident records alone do not establish an all-clear.";
  if (resource.status === "error") return "Service operation attention is unavailable; this view covers authorized lane incidents only.";
  const page = resource.data;
  if (!page || !Number.isFinite(Date.parse(page.stale_after)) || now >= Date.parse(page.stale_after)) return "Service operation attention is stale; an all-clear is not established.";
  if (page.has_more || page.completeness !== "single_page") return "Service operation attention is partial; this page cannot establish an all-clear.";
  if (page.items.some(item => !item.original_scope || item.attribution === "corrupt" || item.attribution === "unavailable")) return "Retained work has unknown original ownership and needs attention, even when lanes are healthy.";
  if (page.items.length) return "Retained operations are listed separately below. Healthy lanes do not settle that work.";
  return "No retained service operations were returned in this fresh bounded read. This does not authorize replay or release.";
}
