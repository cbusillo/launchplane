import type { RuntimeSettingRemovalRequest, RuntimeSettingRemovalResponse } from "./generated/openapi.ts";

export type RemovalResponse = RuntimeSettingRemovalResponse;

export function validateRemovalResponse(response: RuntimeSettingRemovalResponse, request: RuntimeSettingRemovalRequest): RemovalResponse {
  const mode = request.mode ?? "dry-run";
  const plan = response.result;
  const sameNames = (left: unknown, right: string[]) => Array.isArray(left) && left.every(name => typeof name === "string") &&
    JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
  if (response.status !== "ok" || !response.trace_id || !plan ||
      plan.product !== request.product || plan.scope !== request.scope ||
      plan.context !== (request.context || "") || plan.instance !== (request.instance || "") ||
      !sameNames(plan.removed_keys, request.keys) ||
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

export type RemovalRecord = Pick<RuntimeSettingRemovalRequest, "product" | "scope" | "context" | "instance">;

export function removalRecordScope(actorId: number, record: RemovalRecord): string {
  return `runtime-setting-removal:${JSON.stringify([actorId, record.scope, record.context || "", record.instance || ""])}`;
}

export function recoverRemovalRecord(storageKey: string): RemovalRecord | null {
  try {
    const record = JSON.parse(sessionStorage.getItem(storageKey) || "null") as RemovalRecord | null;
    return record && typeof record.product === "string" && ["global", "context", "instance"].includes(record.scope) &&
      typeof record.context === "string" && typeof record.instance === "string" ? record : null;
  } catch { return null; }
}
