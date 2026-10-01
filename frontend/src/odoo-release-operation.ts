import { LaunchplaneApiError } from "./api";
import type {
  BrowserOperationFailure,
  BrowserOperationOptions,
  BrowserOperationStorage,
} from "./browser-operation";
import { promotionOperationFailure } from "./promotion-operation";
import type {
  AcceptedEvidenceResponse,
  ProductionBackupGateRequest,
  ProductionBackupGateResponse,
  ReleaseReviewResponse,
  WriteOdooProdPromotionRunData,
} from "./generated/openapi.ts";

// The promotion action the infrastructure backup is recorded against; the
// promotion run refuses a backup taken for any other action.
export const ODOO_PROMOTION_BACKUP_ACTION = "odoo_prod_promotion_run.execute";

export type OdooReleaseStep = "review" | "backup" | "promote";
export type OdooReleaseStepState = "waiting" | "running" | "passed" | "failed";

export interface OdooReleaseScope {
  context: string;
  environment: string;
  product: string;
}

// One click's identity. A retry after an uncertain stop reuses it, so the
// backup enqueue returns the same operation and the promotion replays.
export interface OdooReleaseAttempt {
  idempotencyKey: string;
  requestId: string;
}

export interface OdooReleaseProgress {
  detail: string;
  state: OdooReleaseStepState;
  step: OdooReleaseStep;
}

export type OdooReleaseOutcome =
  | {
      backupRecordId: string;
      response: AcceptedEvidenceResponse;
      status: "promoted";
    }
  | {
      certainty: "definitive" | "uncertain";
      failure: BrowserOperationFailure;
      status: "stopped";
      step: OdooReleaseStep;
    };

export interface OdooReleaseDependencies {
  enqueueBackup: (
    payload: ProductionBackupGateRequest,
    options: BrowserOperationOptions,
  ) => Promise<ProductionBackupGateResponse>;
  promote: (
    payload: WriteOdooProdPromotionRunData["body"],
    options: BrowserOperationOptions,
  ) => Promise<AcceptedEvidenceResponse>;
  readBackup: (
    operationId: string,
    scope: { context: string; instance: string; product: string },
    signal?: AbortSignal,
  ) => Promise<ProductionBackupGateResponse>;
  readReleaseReview: (
    product: string,
    signal?: AbortSignal,
  ) => Promise<ReleaseReviewResponse>;
  wait: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

const BACKUP_ACTIVE_STATUSES = new Set(["pending", "running"]);
// Refusals the server reports with a 5xx status that are still final answers.
const DEFINITIVE_SERVER_CODES = new Set([
  "authorization_provenance_unavailable",
  "database_storage_required",
]);

export function createOdooReleaseAttempt(): OdooReleaseAttempt {
  const id = globalThis.crypto.randomUUID();
  return { idempotencyKey: `ui-odoo-release-${id}`, requestId: `ui-${id}` };
}

export function releaseReviewAllowsPromotion(review: ReleaseReviewResponse): boolean {
  return !review.review.required || review.review.approved;
}

export function odooReleaseFailureCertainty(error: unknown): "definitive" | "uncertain" {
  if (error instanceof LaunchplaneApiError) {
    if (DEFINITIVE_SERVER_CODES.has(error.code)) {
      return "definitive";
    }
    return error.statusCode === 408 || error.statusCode === 429 || error.statusCode >= 500
      ? "uncertain"
      : "definitive";
  }
  return "uncertain";
}

export function odooReleaseFailure(error: unknown): BrowserOperationFailure {
  const failure = promotionOperationFailure(error);
  if (failure.code === "authorization_provenance_unavailable") {
    return {
      ...failure,
      message:
        "Launchplane refused to start the backup: a durable backup needs exactly one managed authorization rule for your identity on this lane, and the administrator role alone does not count.",
    };
  }
  if (failure.code === "authorization_denied") {
    return {
      ...failure,
      message: `Launchplane refused this step for your identity: ${failure.message}`,
    };
  }
  return failure;
}

export async function runOdooRelease({
  attempt,
  dependencies,
  onProgress,
  pollIntervalMilliseconds = 5000,
  scope,
  signal,
}: {
  attempt: OdooReleaseAttempt;
  dependencies: OdooReleaseDependencies;
  onProgress: (progress: OdooReleaseProgress) => void;
  pollIntervalMilliseconds?: number;
  scope: OdooReleaseScope;
  signal?: AbortSignal;
}): Promise<OdooReleaseOutcome> {
  let step: OdooReleaseStep = "review";
  const stop = (
    failure: BrowserOperationFailure,
    certainty: "definitive" | "uncertain",
  ): OdooReleaseOutcome => {
    onProgress({ detail: failure.message, state: "failed", step });
    return { certainty, failure, status: "stopped", step };
  };
  try {
    onProgress({ detail: "Reading the release review.", state: "running", step });
    const review = await dependencies.readReleaseReview(scope.product, signal);
    if (review.product !== scope.product || !releaseReviewAllowsPromotion(review)) {
      const blockers = review.review.blockers.join(" ");
      return stop(
        {
          code: "release_not_approved",
          message: `The release is not approved.${blockers ? ` ${blockers}` : ""}`,
          statusCode: 0,
          traceId: review.trace_id,
        },
        "definitive",
      );
    }
    onProgress({
      detail: review.review.required ? "The release is approved." : "No approval is required.",
      state: "passed",
      step,
    });

    step = "backup";
    onProgress({ detail: "Starting the infrastructure backup.", state: "running", step });
    const backupScope = {
      context: scope.context,
      instance: scope.environment,
      product: scope.product,
    };
    let backup = await dependencies.enqueueBackup(
      {
        ...backupScope,
        backup_record_id: `infrastructure-${scope.context}-${attempt.requestId}`,
        promotion_action: ODOO_PROMOTION_BACKUP_ACTION,
        schema_version: 1,
      },
      { idempotencyKey: `infrastructure-${attempt.idempotencyKey}`, signal },
    );
    while (BACKUP_ACTIVE_STATUSES.has(backup.operation_status)) {
      onProgress({
        detail: `Backup ${backup.operation_status} (operation ${backup.operation_id}).`,
        state: "running",
        step,
      });
      await dependencies.wait(pollIntervalMilliseconds, signal);
      backup = await dependencies.readBackup(backup.operation_id, backupScope, signal);
    }
    if (backup.operation_status !== "pass") {
      return stop(
        {
          code: backup.error_code || `backup_${backup.operation_status}`,
          message: `The backup ended ${backup.operation_status}; nothing was promoted.`,
          statusCode: 0,
          traceId: backup.trace_id,
        },
        "definitive",
      );
    }
    onProgress({
      detail: `Backup ${backup.backup_record_id} verified.`,
      state: "passed",
      step,
    });

    step = "promote";
    onProgress({
      detail: "Promoting the testing artifact to production. This can take many minutes.",
      state: "running",
      step,
    });
    const response = await dependencies.promote(
      {
        product: scope.product,
        run: {
          context: scope.context,
          infrastructure_backup_record_id: backup.backup_record_id,
          request_id: attempt.requestId,
          schema_version: 1,
        },
        schema_version: 1,
      },
      { idempotencyKey: attempt.idempotencyKey, signal },
    );
    const result = response.result ?? {};
    if (result.run_status !== "pass") {
      const message = typeof result.error_message === "string" ? result.error_message : "";
      return stop(
        {
          code: `promotion_${String(result.run_status ?? "unknown")}`,
          message: message || `The promotion ended ${String(result.run_status ?? "without a status")}.`,
          statusCode: 0,
          traceId: response.trace_id,
        },
        "definitive",
      );
    }
    const artifactId = typeof result.artifact_id === "string" ? result.artifact_id : "";
    onProgress({
      detail: artifactId ? `Promoted ${artifactId}.` : "Promoted.",
      state: "passed",
      step,
    });
    return { backupRecordId: backup.backup_record_id, response, status: "promoted" };
  } catch (error) {
    return stop(odooReleaseFailure(error), odooReleaseFailureCertainty(error));
  }
}

export function waitFor(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timeout = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      clearTimeout(timeout);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function readOdooReleaseAttempt(
  scope: OdooReleaseScope,
  storage: BrowserOperationStorage | null = sessionStorageOrNull(),
): OdooReleaseAttempt | null {
  try {
    const value = storage?.getItem(attemptStorageKey(scope));
    if (!value) {
      return null;
    }
    const candidate = JSON.parse(value) as Partial<OdooReleaseAttempt>;
    if (
      typeof candidate.idempotencyKey !== "string" ||
      !candidate.idempotencyKey ||
      typeof candidate.requestId !== "string" ||
      !candidate.requestId
    ) {
      return null;
    }
    return { idempotencyKey: candidate.idempotencyKey, requestId: candidate.requestId };
  } catch {
    return null;
  }
}

export function writeOdooReleaseAttempt(
  scope: OdooReleaseScope,
  attempt: OdooReleaseAttempt | null,
  storage: BrowserOperationStorage | null = sessionStorageOrNull(),
): void {
  try {
    if (attempt) {
      storage?.setItem(attemptStorageKey(scope), JSON.stringify(attempt));
    } else {
      storage?.removeItem(attemptStorageKey(scope));
    }
  } catch {
    return;
  }
}

function attemptStorageKey(scope: OdooReleaseScope): string {
  return `launchplane.odoo-release.${scope.product.trim()}.${scope.environment.trim()}`;
}

function sessionStorageOrNull(): BrowserOperationStorage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
