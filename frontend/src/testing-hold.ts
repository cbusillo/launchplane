import type { ApplyTestingHoldData } from "./generated/openapi.ts";
import { safeExternalUrl } from "./url";

export type TestingHoldRequest = ApplyTestingHoldData["body"];
export type TestingHoldScope = Pick<TestingHoldRequest, "product" | "context" | "instance">;
export interface TestingHold {
  reason: string;
  recorded_by: string;
  recorded_at: string;
  issue_url?: string;
  purpose?: "staff_testing" | "isolation_staging";
}
export interface TestingHoldReview {
  before: TestingHold;
  plan_sha256: string;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Testing hold evidence is unavailable.");
  return value as Record<string, unknown>;
}
function scopedResult(response: { result: unknown }, scope: TestingHoldScope) {
  const result = object(response.result);
  if (result.product !== scope.product || result.context !== scope.context || result.instance !== scope.instance) {
    throw new Error("Testing hold evidence belongs to a different lane.");
  }
  return result;
}
function checkedHold(value: unknown): TestingHold {
  const hold = object(value);
  if (typeof hold.reason !== "string" || !hold.reason.trim() || typeof hold.recorded_by !== "string" || typeof hold.recorded_at !== "string" ||
      hold.issue_url !== undefined && typeof hold.issue_url !== "string" ||
      hold.purpose !== undefined && hold.purpose !== "staff_testing" && hold.purpose !== "isolation_staging") {
    throw new Error("Testing hold evidence is incomplete.");
  }
  return hold as unknown as TestingHold;
}
export function testingHoldFromResponse(response: { result: unknown }, scope: TestingHoldScope): TestingHold | null {
  const result = scopedResult(response, scope);
  return result.hold === null ? null : checkedHold(result.hold);
}
export function testingHoldPlanFromResponse(response: { result: unknown }, scope: TestingHoldScope): TestingHoldReview {
  const result = scopedResult(response, scope);
  const before = checkedHold(result.before);
  if (result.action !== "clear" || result.changed !== true || result.applied !== false ||
      before.purpose === "isolation_staging" || typeof result.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.plan_sha256)) {
    throw new Error("Launchplane did not return an ordinary testing hold lift to review.");
  }
  return { before, plan_sha256: result.plan_sha256 };
}
export function testingHoldIssueLink(hold: TestingHold): string | null {
  const url = safeExternalUrl(hold.issue_url ?? "");
  return url?.protocol === "https:" && !url.username && !url.password ? url.href : null;
}
export function restoredTestingHoldRequest(raw: string | null, scope: TestingHoldScope): TestingHoldRequest | null {
  try {
    const request = object(JSON.parse(raw ?? "null"));
    if (request.product === scope.product && request.context === scope.context && request.instance === "testing" && scope.instance === "testing" &&
        request.mode === "apply" && request.hold === false && !request.rehearsal && !request.activation_plan_sha256 &&
        (request.purpose === undefined || request.purpose === "staff_testing") &&
        typeof request.reason === "string" && request.reason.trim() && typeof request.reviewed_plan_sha256 === "string" && /^[a-f0-9]{64}$/.test(request.reviewed_plan_sha256)) {
      return request as TestingHoldRequest;
    }
  } catch { /* Invalid or inaccessible browser recovery data grants no authority. */ }
  return null;
}
