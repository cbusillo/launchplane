import type {
  BrowserRouteRepairRequest, DokployDomainRepairReview, EnvironmentRouteBindingReadModel,
  RouteBindingExpectedCurrent, RouteBindingRepairReview,
  SetupDokployTargetResponse, ReconcileRouteBindingResponse,
} from "./generated/openapi.ts";

export type RepairKind = "domain" | "binding";
export interface RepairScope { product: string; context: string; instance: string }
export type RepairReview = DokployDomainRepairReview | RouteBindingRepairReview;
export type RepairResponse = SetupDokployTargetResponse | ReconcileRouteBindingResponse;
interface CommonRequest {
  mode: "dry-run" | "apply"; product: string; context: string; instance: string;
  reason: string; confirmation: string; browser_review: BrowserRouteRepairRequest;
}
export interface DomainRepairRequest extends CommonRequest {
  product: "launchplane"; operation: "repair-domain-authority";
}
export interface BindingRepairRequest extends CommonRequest { expected_current: RouteBindingExpectedCurrent }
export type RepairRequest = DomainRepairRequest | BindingRepairRequest;

export function repairOperationScope(scope: RepairScope, kind: RepairKind): string {
  return JSON.stringify([scope.product, scope.context, scope.instance, "route-record-repair", kind]);
}
export function repairStorageKey(scope: string): string { return `launchplane:route-repair-request:${scope}`; }
export function repairConfirmation(kind: RepairKind): string {
  return kind === "domain" ? "APPLY DOKPLOY TARGET SETUP" : "APPLY LAUNCHPLANE ROUTE BINDING RECONCILE";
}
export function repairRequest(scope: RepairScope, kind: RepairKind, reason: string, review?: RepairReview): RepairRequest {
  const common = {
    context: scope.context, instance: scope.instance, reason: reason.trim(),
    mode: review ? "apply" as const : "dry-run" as const,
    confirmation: review ? repairConfirmation(kind) : "",
    browser_review: { product: scope.product, reviewed_plan_sha256: review?.plan_sha256 ?? "" },
  };
  return kind === "domain" ? { ...common, product: "launchplane", operation: "repair-domain-authority" }
    : { ...common, product: scope.product, expected_current: review?.operation === "reconcile-route-binding"
      ? review.expected_current : { state: "absent", record_sha256: "" } };
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function strings(value: unknown): value is string[] { return Array.isArray(value) && value.every(item => typeof item === "string"); }
function digest(value: unknown): value is string { return typeof value === "string" && /^[a-f0-9]{64}$/.test(value); }
function matchesScope(value: Record<string, unknown>, scope: RepairScope): boolean {
  return value.product === scope.product && value.context === scope.context && value.instance === scope.instance;
}
function expectedCurrent(value: unknown): value is RouteBindingExpectedCurrent {
  return object(value) && exactKeys(value, ["state", "record_sha256"]) &&
    (value.state === "absent" ? value.record_sha256 === "" : value.state === "present" && digest(value.record_sha256));
}

// A retained payload is untrusted browser storage. Never turn malformed recovery into a new apply.
export function parseStoredRepairRequest(value: string | null, scope: RepairScope, kind: RepairKind): RepairRequest | null {
  try {
    const request: unknown = JSON.parse(value ?? "null");
    if (!object(request) || !exactKeys(request, ["mode", "product", "context", "instance", "reason", "confirmation", "browser_review",
      kind === "domain" ? "operation" : "expected_current"]) || request.mode !== "apply" ||
      request.context !== scope.context || request.instance !== scope.instance ||
      typeof request.reason !== "string" || !request.reason.trim() || request.reason !== request.reason.trim() ||
      request.confirmation !== repairConfirmation(kind) || !object(request.browser_review) ||
      !exactKeys(request.browser_review, ["product", "reviewed_plan_sha256"]) ||
      request.browser_review.product !== scope.product || !digest(request.browser_review.reviewed_plan_sha256)) return null;
    if (kind === "domain") {
      if (request.product !== "launchplane" || request.operation !== "repair-domain-authority") return null;
      return { mode: "apply", product: "launchplane", operation: "repair-domain-authority", context: scope.context, instance: scope.instance,
        reason: request.reason, confirmation: request.confirmation,
        browser_review: { product: scope.product, reviewed_plan_sha256: request.browser_review.reviewed_plan_sha256 } };
    }
    if (request.product !== scope.product || !expectedCurrent(request.expected_current)) return null;
    return { mode: "apply", product: scope.product, context: scope.context, instance: scope.instance, reason: request.reason,
      confirmation: request.confirmation, browser_review: { product: scope.product, reviewed_plan_sha256: request.browser_review.reviewed_plan_sha256 },
      expected_current: request.expected_current };
  } catch { return null; }
}

function recordValid(value: unknown, scope: RepairScope): value is EnvironmentRouteBindingReadModel {
  return object(value) && matchesScope(value, scope) && digest(value.record_sha256) &&
    object(value.provider_target) && typeof value.provider_target.target_name === "string" &&
    object(value.ingress) && typeof value.ingress.provider === "string" && typeof value.ingress.endpoint_key === "string" &&
    object(value.tls) && typeof value.tls.owner === "string";
}
export function checkedRepairReview(response: RepairResponse, scope: RepairScope, kind: RepairKind): RepairReview {
  const value: unknown = response.review;
  if (response.status !== "accepted" || typeof response.trace_id !== "string" || !response.trace_id || !object(value) ||
      !matchesScope(value, scope) || !digest(value.plan_sha256) || typeof value.applied !== "boolean" ||
      !strings(value.current_domains) || !strings(value.proposed_domains) || !Array.isArray(value.source_records) ||
      !value.source_records.every(source => object(source) && typeof source.identity === "string" && typeof source.version === "string")) {
    throw new Error("The service response does not verify this lane's route-record review.");
  }
  if (kind === "domain" && value.operation === "repair-domain-authority" && ["ready", "unchanged"].includes(String(value.status)) &&
      ["application", "compose"].includes(String(value.target_type)) && typeof value.target_id === "string" && !!value.target_id &&
      typeof value.target_name === "string" && strings(value.observed_domains) && typeof value.proposed_primary_domain === "string") {
    return response.review as DokployDomainRepairReview;
  }
  if (kind === "binding" && value.operation === "reconcile-route-binding" && ["ready", "unchanged", "blocked", "conflict"].includes(String(value.status)) &&
      ["create", "refresh", "none"].includes(String(value.binding_operation)) && expectedCurrent(value.expected_current) &&
      typeof value.current_record_sha256 === "string" && Array.isArray(value.findings) &&
      value.findings.every(finding => object(finding) && typeof finding.code === "string" && typeof finding.detail === "string") &&
      (value.current_record == null ? value.current_record_sha256 === "" : recordValid(value.current_record, scope) && value.current_record.record_sha256 === value.current_record_sha256) &&
      (value.proposed_record == null || recordValid(value.proposed_record, scope))) {
    return response.review as RouteBindingRepairReview;
  }
  throw new Error("The service response does not verify the requested route-record operation.");
}

export function verifyRepairReceipt(response: RepairResponse, scope: RepairScope, kind: RepairKind, request: RepairRequest): RepairReview {
  const review = checkedRepairReview(response, scope, kind);
  if (response.result?.mode !== "apply" || !review.applied || !["ready", "unchanged"].includes(review.status) ||
      review.plan_sha256 !== request.browser_review.reviewed_plan_sha256 ||
      (review.operation === "reconcile-route-binding" && (!review.proposed_record || !("expected_current" in request) ||
        review.expected_current.state !== request.expected_current.state || review.expected_current.record_sha256 !== request.expected_current.record_sha256))) {
    throw new Error("The response does not verify the original reviewed apply. Preserve its request and key.");
  }
  return review;
}

export function repairReadbackMatches(applied: RepairReview, current: RepairReview): boolean {
  if (applied.operation === "repair-domain-authority" && current.operation === applied.operation) {
    return current.target_id === applied.target_id && current.target_type === applied.target_type && current.target_name === applied.target_name &&
      JSON.stringify(current.current_domains) === JSON.stringify(applied.proposed_domains);
  }
  return applied.operation === "reconcile-route-binding" && current.operation === applied.operation && !!applied.proposed_record &&
    current.current_record_sha256 === applied.proposed_record.record_sha256;
}
