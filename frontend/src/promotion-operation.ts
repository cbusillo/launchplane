import { LaunchplaneApiError } from "./api";
import type {
  BrowserOperationFailure,
  BrowserOperationStorage,
} from "./browser-operation";
import type {
  ProductPromotionDryRunResponse,
  ProductPromotionStatus,
  ProductPromotionWorkflowDeliveryStatus,
  ProductPromotionWorkflowDispatchResponse,
} from "./generated/openapi.ts";

export type PromotionBump = "patch" | "minor" | "major";

export interface StoredPromotionDirectRequest {
  schema_version?: number;
  reason: string;
  evidence_fingerprint: string;
  bump: PromotionBump;
}

export interface StoredPromotionWorkflowRequest {
  schema_version?: number;
  dry_run: boolean;
  reason: string;
  evidence_fingerprint: string;
  bump: PromotionBump;
  confirmation: string;
}

export interface StoredPromotionDraft {
  acceptedWorkflow: ProductPromotionWorkflowDispatchResponse | null;
  bump: PromotionBump;
  reason: string;
  pendingDirect: StoredPromotionDirectRequest | null;
  pendingWorkflow: StoredPromotionWorkflowRequest | null;
}

export function promotionPlanMatchesStatus(
  plan: ProductPromotionDryRunResponse | null,
  status: ProductPromotionStatus,
  bump: PromotionBump,
): boolean {
  if (!plan) {
    return false;
  }
  return (
    plan.result.evidence_fingerprint === status.evidence_fingerprint &&
    plan.result.bump === bump &&
    plan.result.artifact_id === status.source.artifact_id &&
    plan.result.source_git_ref === status.source.source_git_ref &&
    plan.result.product === status.product &&
    plan.result.to_instance === status.destination_environment
  );
}

export function promotionLiveConfirmation(
  status: ProductPromotionStatus,
  bump: PromotionBump,
): string {
  return status.live_confirmations[bump];
}

export function promotionOperationFailure(error: unknown): BrowserOperationFailure {
  if (error instanceof LaunchplaneApiError) {
    return {
      code: error.code || "request_failed",
      message: error.message,
      statusCode: error.statusCode,
      traceId: error.traceId,
    };
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return {
      code: "request_cancelled",
      message: "The browser stopped waiting for the promotion operation.",
      statusCode: 0,
      traceId: "",
    };
  }
  return {
    code: "request_failed",
    message: error instanceof Error ? error.message : "Promotion request failed.",
    statusCode: 0,
    traceId: "",
  };
}

export function promotionFailureCertainty(
  error: unknown,
  dispatched: boolean,
): "definitive" | "uncertain" {
  if (error instanceof LaunchplaneApiError) {
    if (
      dispatched &&
      (error.statusCode === 408 || error.statusCode === 429 || error.statusCode >= 500)
    ) {
      return "uncertain";
    }
    return "definitive";
  }
  return dispatched ? "uncertain" : "definitive";
}

export function promotionDeliveryNeedsRefresh(
  delivery: ProductPromotionWorkflowDeliveryStatus | null,
): boolean {
  if (!delivery) {
    return false;
  }
  return (
    delivery.state === "pending" ||
    delivery.state === "running" ||
    delivery.state === "reconcile_required"
  );
}

export function readPromotionDraft(
  product: string,
  environment: string,
  storage: BrowserOperationStorage | null = promotionSessionStorage(),
): StoredPromotionDraft | null {
  if (!storage) {
    return null;
  }
  try {
    const value = storage.getItem(promotionDraftStorageKey(product, environment));
    if (!value) {
      return null;
    }
    const candidate = JSON.parse(value) as Partial<StoredPromotionDraft>;
    if (!isPromotionBump(candidate.bump) || typeof candidate.reason !== "string") {
      return null;
    }
    const acceptedWorkflow = candidate.acceptedWorkflow ?? null;
    if (acceptedWorkflow !== null && !isStoredWorkflowResponse(acceptedWorkflow)) {
      return null;
    }
    const pendingDirect = candidate.pendingDirect ?? null;
    if (pendingDirect !== null && !isStoredDirectRequest(pendingDirect)) {
      return null;
    }
    const pendingWorkflow = candidate.pendingWorkflow ?? null;
    if (pendingWorkflow !== null && !isStoredWorkflowRequest(pendingWorkflow)) {
      return null;
    }
    return {
      acceptedWorkflow,
      bump: candidate.bump,
      reason: candidate.reason,
      pendingDirect,
      pendingWorkflow,
    };
  } catch {
    return null;
  }
}

export function writePromotionDraft(
  product: string,
  environment: string,
  draft: StoredPromotionDraft,
  storage: BrowserOperationStorage | null = promotionSessionStorage(),
): void {
  if (!storage) {
    return;
  }
  try {
    storage.setItem(
      promotionDraftStorageKey(product, environment),
      JSON.stringify(draft),
    );
  } catch {
    return;
  }
}

export function clearPromotionDraft(
  product: string,
  environment: string,
  storage: BrowserOperationStorage | null = promotionSessionStorage(),
): void {
  if (!storage) {
    return;
  }
  try {
    storage.removeItem(promotionDraftStorageKey(product, environment));
  } catch {
    return;
  }
}

function promotionDraftStorageKey(product: string, environment: string): string {
  return `launchplane.promotion-draft.${product.trim()}.${environment.trim()}`;
}

function isPromotionBump(value: unknown): value is PromotionBump {
  return value === "patch" || value === "minor" || value === "major";
}

function isStoredDirectRequest(value: unknown): value is StoredPromotionDirectRequest {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<StoredPromotionDirectRequest>;
  return (
    (candidate.schema_version === undefined ||
      typeof candidate.schema_version === "number") &&
    typeof candidate.reason === "string" &&
    typeof candidate.evidence_fingerprint === "string" &&
    isPromotionBump(candidate.bump)
  );
}

function isStoredWorkflowRequest(value: unknown): value is StoredPromotionWorkflowRequest {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<StoredPromotionWorkflowRequest>;
  return (
    (candidate.schema_version === undefined ||
      typeof candidate.schema_version === "number") &&
    typeof candidate.dry_run === "boolean" &&
    typeof candidate.reason === "string" &&
    typeof candidate.evidence_fingerprint === "string" &&
    isPromotionBump(candidate.bump) &&
    typeof candidate.confirmation === "string"
  );
}

function isStoredWorkflowResponse(
  value: unknown,
): value is ProductPromotionWorkflowDispatchResponse {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<ProductPromotionWorkflowDispatchResponse>;
  const result = candidate.result;
  return (
    candidate.status === "accepted" &&
    typeof candidate.trace_id === "string" &&
    !!result &&
    typeof result === "object" &&
    typeof result.delivery_id === "string" &&
    result.delivery_id.length > 0 &&
    typeof result.dry_run === "boolean" &&
    isPromotionBump(result.bump)
  );
}

function promotionSessionStorage(): BrowserOperationStorage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
