import { LaunchplaneApiError } from "./api";
import type { BrowserOperationFailureCertainty } from "./browser-operation";
import type { RuntimeSyncReview, RuntimeSyncResponse } from "./generated/openapi.ts";

export interface RuntimeSyncScope { product: string; context: string; instance: string }
export interface RuntimeSyncRequest extends RuntimeSyncScope {
  mode: "dry-run" | "apply";
  browser_review: { reviewed_plan_sha256?: string };
}
export interface PendingRuntimeSync { request: RuntimeSyncRequest; review: RuntimeSyncReview }

export function runtimeSyncScope(scope: RuntimeSyncScope): string {
  return JSON.stringify([scope.product, scope.context, scope.instance, "runtime-sync"]);
}
export function runtimeSyncStorageKey(scope: RuntimeSyncScope): string {
  return `launchplane:runtime-sync-request:${runtimeSyncScope(scope)}`;
}
export function runtimeSyncRequest(scope: RuntimeSyncScope, review?: RuntimeSyncReview): RuntimeSyncRequest {
  return { ...scope, mode: review ? "apply" : "dry-run",
    browser_review: review ? { reviewed_plan_sha256: review.plan_sha256 } : {} };
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string" && !!item);
}
function digest(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}
function matchesScope(value: Record<string, unknown>, scope: RuntimeSyncScope): boolean {
  return value.product === scope.product && value.context === scope.context && value.instance === scope.instance;
}
function validReview(value: unknown, scope: RuntimeSyncScope): value is RuntimeSyncReview {
  return object(value) && exactKeys(value, ["operation", "product", "context", "instance", "plan_sha256", "target_fingerprint",
    "target_id", "target_type", "target_name", "missing_keys", "different_keys", "retired_keys_present", "changed_keys",
    "desired_key_count", "live_key_count", "unchanged_key_count", "applied"]) && matchesScope(value, scope) && value.operation === "runtime-sync" &&
    digest(value.plan_sha256) && typeof value.target_fingerprint === "string" && !!value.target_fingerprint &&
    typeof value.target_id === "string" && !!value.target_id && typeof value.target_name === "string" && !!value.target_name &&
    ["application", "compose"].includes(String(value.target_type)) && typeof value.applied === "boolean" &&
    ["missing_keys", "different_keys", "retired_keys_present", "changed_keys"].every(key => strings(value[key])) &&
    ["desired_key_count", "live_key_count", "unchanged_key_count"].every(key =>
      typeof value[key] === "number" && Number.isSafeInteger(value[key]) && value[key] >= 0);
}

export function checkedRuntimeSyncReview(response: RuntimeSyncResponse, scope: RuntimeSyncScope): RuntimeSyncReview {
  if (response.status !== "accepted" || typeof response.trace_id !== "string" || !response.trace_id || !validReview(response.review, scope) ||
      !object(response.result) || response.result.mode !== (response.review.applied ? "apply" : "dry-run")) {
    throw new Error("The service response does not verify this lane's runtime sync review.");
  }
  return response.review;
}

// Recovery is untrusted browser storage, never a source for a newly invented apply.
export function parsePendingRuntimeSync(value: string | null, scope: RuntimeSyncScope): PendingRuntimeSync | null {
  try {
    const saved: unknown = JSON.parse(value ?? "null");
    if (!object(saved) || !exactKeys(saved, ["request", "review"]) || !object(saved.request) ||
        !exactKeys(saved.request, ["mode", "product", "context", "instance", "browser_review"]) ||
        !matchesScope(saved.request, scope) || saved.request.mode !== "apply" ||
        !object(saved.request.browser_review) || !exactKeys(saved.request.browser_review, ["reviewed_plan_sha256"]) ||
        !digest(saved.request.browser_review.reviewed_plan_sha256) || !validReview(saved.review, scope) || saved.review.applied ||
        saved.review.plan_sha256 !== saved.request.browser_review.reviewed_plan_sha256) return null;
    return { request: runtimeSyncRequest(scope, saved.review), review: saved.review };
  } catch { return null; }
}

export function verifyRuntimeSyncReceipt(response: RuntimeSyncResponse, scope: RuntimeSyncScope,
  pending: PendingRuntimeSync): RuntimeSyncReview {
  const review = checkedRuntimeSyncReview(response, scope);
  const original = pending.review;
  const result = response.result;
  if (!object(result) || result.mode !== "apply" || !object(result.apply) || result.apply.applied !== true ||
      !object(result.apply.verification) || result.apply.verification.status !== "pass" ||
      (original.changed_keys.length > 0 && result.apply.update_acknowledged !== true) ||
      !object(result.deploy) || result.deploy.requested !== false || result.deploy.triggered !== false ||
      !review.applied || review.plan_sha256 !== pending.request.browser_review.reviewed_plan_sha256 ||
      review.target_id !== original.target_id || review.target_type !== original.target_type ||
      review.target_name !== original.target_name || review.target_fingerprint !== original.target_fingerprint) {
    throw new Error("The response does not verify the original reviewed sync. Preserve its request and key.");
  }
  return review;
}

export function runtimeSyncReadbackMatches(applied: RuntimeSyncReview, current: RuntimeSyncReview): boolean {
  return !current.applied && current.product === applied.product && current.context === applied.context && current.instance === applied.instance &&
    current.target_id === applied.target_id && current.target_type === applied.target_type && current.target_name === applied.target_name &&
    current.changed_keys.length === 0;
}

export function runtimeSyncReceiptState(response: RuntimeSyncResponse): "recovered" | "verified" {
  return object(response.result) && object(response.result.apply) && response.result.apply.recovered_by_readback === true
    ? "recovered" : "verified";
}

export function runtimeSyncFailureCertainty(error: unknown, dispatched: boolean): BrowserOperationFailureCertainty {
  if (!dispatched) return "definitive";
  if (error instanceof LaunchplaneApiError) {
    if (error.statusCode >= 500) return "uncertain";
    if (error.statusCode === 409 && ["runtime_sync_target_busy", "idempotency_key_reused", "runtime_sync_lane_unqualified"].includes(error.code)) return "definitive";
    // These route-specific codes replay this original key's durable terminal outcome.
    if (error.statusCode === 409 && ["stale", "runtime_sync_not_applied", "runtime_sync_provider_refused", "runtime_sync_unverified"].includes(error.code)) return "settled";
    if (["authentication_required", "authorization_denied", "browser_mutation_denied", "invalid_request", "idempotency_key_required"].includes(error.code)) {
      return "definitive";
    }
  }
  return "uncertain";
}
