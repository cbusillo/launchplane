import type { RuntimeSettingRemovalRequest, RuntimeSettingRemovalResponse } from "./generated/openapi.ts";

export interface RemovalPlan {
  product: string;
  scope: string;
  context: string;
  instance: string;
  removed_keys: string[];
  surviving_keys: string[];
  surviving_value_count: number;
  unprintable_survivor_count: number;
  retirement_intent_count: number;
  record_deleted: boolean;
  requires_compatible_readers_and_rollback: boolean;
  review_id: string;
  plan_sha256: string;
  applied: boolean;
  readback_matches?: boolean;
}

export type RemovalResponse = Omit<RuntimeSettingRemovalResponse, "result"> & { result: RemovalPlan };

export function validateRemovalResponse(response: RuntimeSettingRemovalResponse, request: RuntimeSettingRemovalRequest): RemovalResponse {
  const mode = request.mode ?? "dry-run";
  const plan = response.result as unknown as RemovalPlan;
  const sameNames = (left: unknown, right: string[]) => Array.isArray(left) && left.every(name => typeof name === "string") &&
    JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
  if (response.status !== "ok" || !response.trace_id || !plan ||
      plan.product !== request.product || plan.scope !== request.scope ||
      plan.context !== (request.context || "") || plan.instance !== (request.instance || "") ||
      !sameNames(plan.removed_keys, request.keys) ||
      !Array.isArray(plan.surviving_keys) || !plan.surviving_keys.every(key => typeof key === "string") ||
      ![plan.surviving_value_count, plan.unprintable_survivor_count, plan.retirement_intent_count].every(count => Number.isSafeInteger(count) && count >= 0) ||
      plan.surviving_value_count !== plan.surviving_keys.length + plan.unprintable_survivor_count ||
      plan.record_deleted !== (plan.surviving_value_count === 0 && plan.retirement_intent_count === 0) ||
      plan.requires_compatible_readers_and_rollback !== (plan.surviving_value_count === 0 && plan.retirement_intent_count > 0) ||
      !/^[a-f0-9]{64}$/.test(plan.plan_sha256) || plan.review_id !== `runtime-removal-${plan.plan_sha256}` ||
      plan.applied !== (mode === "apply") ||
      (mode !== "dry-run" && (plan.review_id !== request.review_id || plan.plan_sha256 !== request.reviewed_plan_sha256)) ||
      (mode === "readback" && typeof plan.readback_matches !== "boolean")) {
    throw new Error("The removal response does not match the exact request. Keep the original request and operation key if Apply was sent.");
  }
  return { ...response, result: plan };
}

export function recoverRemovalRequest(storageKey: string): RuntimeSettingRemovalRequest | null {
  try {
    const request = JSON.parse(sessionStorage.getItem(storageKey) || "null") as RuntimeSettingRemovalRequest | null;
    return request?.mode === "apply" && typeof request.reason === "string" && Array.isArray(request.keys) &&
      /^[a-f0-9]{64}$/.test(request.reviewed_plan_sha256 || "") &&
      request.review_id === `runtime-removal-${request.reviewed_plan_sha256}` ? request : null;
  } catch { return null; }
}
