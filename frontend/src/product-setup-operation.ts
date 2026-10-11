import type { AcceptedEvidenceResponse, ApplyProductExpectedConfigData, ApplyProductStableLaneRepairData, ProductLaneProfile } from "./generated/openapi.ts";

export type SetupRequest = ApplyProductExpectedConfigData["body"] | ApplyProductStableLaneRepairData["body"];
export type SetupKind = "environment" | "declarations";
export interface BindingDraft { binding_key: string; integration: string }

export function environmentSetupRequest(product: string, context: string, instance: string, baseUrl: string, reason: string): ApplyProductStableLaneRepairData["body"] {
  const lane = instance.trim();
  if (!lane || ["prod", "production"].includes(lane.toLowerCase())) throw new Error("Choose a non-production environment.");
  if (!context.trim() || !reason.trim()) throw new Error("Enter the product context and a reason.");
  const url = new URL(baseUrl.trim());
  if (url.protocol !== "https:" || url.username || url.password || url.port || url.search || url.hash || url.pathname !== "/") throw new Error("Enter the prepared target's HTTPS origin without a path or credentials.");
  return { schema_version: 1, mode: "dry-run", product, context: context.trim(), instance: lane, base_url: url.origin, reason: reason.trim() };
}

export function declarationSetupRequest(product: string, lane: ProductLaneProfile, bindings: BindingDraft[], reason: string): ApplyProductExpectedConfigData["body"] {
  if (["prod", "production"].includes(lane.instance.toLowerCase())) throw new Error("Choose a non-production environment.");
  if (!lane.context || !lane.instance || !reason.trim()) throw new Error("Select an environment and enter a reason.");
  const requirements = bindings.map(item => ({ binding_key: item.binding_key.trim(), integration: item.integration.trim(), context: lane.context, instance: lane.instance, owner_input: null }));
  if (!requirements.length || requirements.some(item => !item.binding_key || !item.integration)) throw new Error("Enter a binding key and integration for every declaration.");
  if (new Set(requirements.map(item => JSON.stringify([item.binding_key, item.integration]))).size !== requirements.length) throw new Error("Each binding key and integration must be unique.");
  return { schema_version: 1, mode: "dry-run", product, reason: reason.trim(), managed_secret_bindings: requirements };
}

export function reviewedSetupApply(kind: SetupKind, request: SetupRequest, response: AcceptedEvidenceResponse): SetupRequest {
  const result = response.result;
  if (!result || result.status !== "ok" || result.mode !== "dry-run" || result.product !== request.product || typeof result.changed !== "boolean") throw new Error("The service did not return a reviewable dry-run. Run it again.");
  if (kind === "environment") {
    if (!("instance" in request) || result.instance !== request.instance || result.context !== request.context || result.base_url !== request.base_url || typeof result.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.plan_sha256)) throw new Error("The service plan does not match this environment.");
    return { ...request, mode: "apply", reviewed_plan_sha256: result.plan_sha256 };
  }
  const rows = setupDeclarationDiff(response);
  const identity = (row: { binding_key: string; integration: string; context: string; instance: string }) => JSON.stringify([row.binding_key, row.integration, row.context, row.instance]);
  if (!("managed_secret_bindings" in request) || JSON.stringify(rows.map(identity).sort()) !== JSON.stringify(request.managed_secret_bindings?.map(identity).sort())) throw new Error("The service diff does not match the requested declarations.");
  return { ...request, mode: "apply" };
}

export function verifySetupReadback(kind: SetupKind, request: SetupRequest, profile: import("./generated/openapi.ts").LaunchplaneProductProfileRecord): void {
  if (profile.product !== request.product) throw new Error("The product setup read-back differs from the reviewed product.");
  if (kind === "environment" && "instance" in request) {
    if (!profile.lanes.some(lane => lane.instance === request.instance && lane.context === request.context && lane.base_url === request.base_url)) throw new Error("The environment read-back differs from the reviewed change.");
  } else if (kind === "declarations" && "managed_secret_bindings" in request) {
    if (!request.managed_secret_bindings?.every(required => profile.expected_config.managed_secret_bindings.some(stored => stored.binding_key === required.binding_key && stored.integration === required.integration && stored.context === required.context && stored.instance === required.instance))) throw new Error("The declarations were not verified in the product read-back.");
  } else throw new Error("The setup request does not match its read-back.");
}

export function setupDeclarationDiff(response: AcceptedEvidenceResponse): Array<{ binding_key: string; integration: string; context: string; instance: string; change: string }> {
  const diff = response.result?.managed_secret_bindings as Record<string, unknown> | undefined;
  if (!diff || !Array.isArray(diff.added) || !Array.isArray(diff.unchanged)) throw new Error("The service did not return the declaration diff.");
  return ["added", "unchanged"].flatMap(change => (diff[change] as unknown[]).map(value => {
    if (!value || typeof value !== "object") throw new Error("The service declaration diff is unreadable.");
    const row = value as Record<string, unknown>;
    if (!["binding_key", "integration", "context", "instance"].every(key => typeof row[key] === "string")) throw new Error("The service declaration scope is unreadable.");
    return { binding_key: row.binding_key as string, integration: row.integration as string, context: row.context as string, instance: row.instance as string, change };
  }));
}
