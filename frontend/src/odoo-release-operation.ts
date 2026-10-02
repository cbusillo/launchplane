import { LaunchplaneApiError } from "./api";
import type {
  BrowserOperationFailure,
  BrowserOperationOptions,
  BrowserOperationStorage,
} from "./browser-operation";
import { promotionOperationFailure } from "./promotion-operation";
import type {
  EnqueueOdooProdPromotionData,
  EnqueueOdooProdRollbackData,
  OdooProdRollbackOperationResponse,
  OdooProdRollbackOperationView,
  OdooProdPromotionOperationResponse,
  OdooProdPromotionOperationView,
  OdooProdPromotionRunResult,
  ProductionBackupGateRequest,
  ProductionBackupGateResponse,
  ReleaseReviewResponse,
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
// backup and promotion enqueues return the operations they already created.
// Once the promotion is queued its operation id is kept too, and a reload only
// resumes watching that operation.
export interface OdooReleaseAttempt {
  idempotencyKey: string;
  promotionOperationId?: string;
  requestId: string;
}

export interface OdooReleaseProgress {
  detail: string;
  state: OdooReleaseStepState;
  step: OdooReleaseStep;
}

export type OdooReleaseOutcome =
  | {
      operation: OdooProdPromotionOperationView;
      result: OdooProdPromotionRunResult;
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
  enqueuePromotion: (
    payload: EnqueueOdooProdPromotionData["body"],
    options: BrowserOperationOptions,
  ) => Promise<OdooProdPromotionOperationResponse>;
  readPromotion: (
    operationId: string,
    scope: { context: string; product: string },
    signal?: AbortSignal,
  ) => Promise<OdooProdPromotionOperationResponse>;
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
const RELEASE_ACTIVE_STATUSES = new Set(["pending", "running"]);
const PROMOTION_PHASE_DETAILS: Record<string, string> = {
  created: "Queued; waiting for the Launchplane worker.",
  running: "The worker picked it up and is checking approval and the backup.",
  validated: "Approval and backup verified; starting the logical database backup.",
  logical_backup_started: "Taking the logical database backup.",
  logical_backup_completed: "Logical backup taken.",
  promotion_started: "Deploying the testing artifact and running post-deploy. This can take many minutes.",
};
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
        "Launchplane could not record durable authority for your identity on this lane: it needs you to be an admin, or to have exactly one managed rule for this action.",
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
  onPromotionQueued,
  pollIntervalMilliseconds = 5000,
  scope,
  signal,
}: {
  attempt: OdooReleaseAttempt;
  dependencies: OdooReleaseDependencies;
  onProgress: (progress: OdooReleaseProgress) => void;
  onPromotionQueued?: (operationId: string) => void;
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
  const promotionScope = { context: scope.context, product: scope.product };
  try {
    if (attempt.promotionOperationId) {
      // The promotion is already queued: only watch it. Re-running review and
      // backup would ask for a second backup the server would not use.
      for (const earlier of ["review", "backup"] as const) {
        onProgress({ detail: "Done before the promotion was queued.", state: "passed", step: earlier });
      }
      step = "promote";
      onProgress({ detail: "Reading the queued promotion.", state: "running", step });
      const resumed = await dependencies.readPromotion(
        attempt.promotionOperationId,
        promotionScope,
        signal,
      );
      return await watchPromotion(resumed);
    }
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
    onProgress({ detail: "Queueing the promotion.", state: "running", step });
    const queued = await dependencies.enqueuePromotion(
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
    onPromotionQueued?.(queued.operation.operation_id);
    return await watchPromotion(queued);
  } catch (error) {
    return stop(odooReleaseFailure(error), odooReleaseFailureCertainty(error));
  }

  async function watchPromotion(
    response: OdooProdPromotionOperationResponse,
  ): Promise<OdooReleaseOutcome> {
    let current = response;
    while (RELEASE_ACTIVE_STATUSES.has(current.operation.status)) {
      onProgress({
        detail: PROMOTION_PHASE_DETAILS[current.operation.phase] ?? `Phase ${current.operation.phase}.`,
        state: "running",
        step,
      });
      await dependencies.wait(pollIntervalMilliseconds, signal);
      current = await dependencies.readPromotion(
        current.operation.operation_id,
        promotionScope,
        signal,
      );
    }
    const operation = current.operation;
    if (operation.status === "pass" && operation.result) {
      onProgress({
        detail: `Promoted ${operation.result.artifact_id || "the testing artifact"}.`,
        state: "passed",
        step,
      });
      return { operation, result: operation.result, status: "promoted" };
    }
    return stop(releaseStopFailure(operation, current.trace_id, "promotion"), "definitive");
  }
}

function releaseStopFailure(
  operation: { error_code: string; error_message: string; operation_id: string; phase: string; result?: { error_message?: string } | null; status: string },
  traceId: string,
  kind: "promotion" | "rollback",
): BrowserOperationFailure {
  if (operation.status === "reconciliation_required") {
    return {
      code: operation.error_code || "operation_reconciliation_required",
      message: `The worker stopped mid-${kind} (phase ${operation.phase}) and did not run it again. Check the latest prod deployment, then cancel operation ${operation.operation_id} with what you found to free the lane.`,
      statusCode: 0,
      traceId,
    };
  }
  if (operation.status === "cancelled") {
    return {
      code: `${kind}_cancelled`,
      message: `The ${kind} ${operation.operation_id} was cancelled before it finished.`,
      statusCode: 0,
      traceId,
    };
  }
  return {
    code: operation.error_code || `${kind}_${operation.status}`,
    message:
      operation.error_message ||
      operation.result?.error_message ||
      `The ${kind} ended ${operation.status}.`,
    statusCode: 0,
    traceId,
  };
}

// One rollback click's identity: a retry reuses the key and reason, so the
// server returns the operation (and the target it fixed) instead of a new one.
export interface OdooRollbackAttempt {
  idempotencyKey: string;
  operationId?: string;
  reason: string;
}

export type OdooRollbackOutcome =
  | { operation: OdooProdRollbackOperationView; status: "rolled_back" }
  | {
      certainty: "definitive" | "uncertain";
      failure: BrowserOperationFailure;
      operation: OdooProdRollbackOperationView | null;
      status: "stopped";
    };

export interface OdooRollbackDependencies {
  enqueueRollback: (
    payload: EnqueueOdooProdRollbackData["body"],
    options: BrowserOperationOptions,
  ) => Promise<OdooProdRollbackOperationResponse>;
  readRollback: (
    operationId: string,
    scope: { context: string; product: string },
    signal?: AbortSignal,
  ) => Promise<OdooProdRollbackOperationResponse>;
  wait: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

const ROLLBACK_PHASE_DETAILS: Record<string, string> = {
  created: "Queued; waiting for the Launchplane worker.",
  running: "The worker picked it up.",
  validated: "Target confirmed; starting the redeploy.",
  rollback_started: "Redeploying the earlier artifact and running post-deploy.",
};

export function createOdooRollbackAttempt(reason: string): OdooRollbackAttempt {
  return { idempotencyKey: `ui-odoo-rollback-${globalThis.crypto.randomUUID()}`, reason };
}

export function rollbackPhaseDetail(phase: string): string {
  return ROLLBACK_PHASE_DETAILS[phase] ?? `Phase ${phase}.`;
}

export async function runOdooRollback({
  attempt,
  dependencies,
  onOperation,
  onQueued,
  pollIntervalMilliseconds = 5000,
  scope,
  signal,
}: {
  attempt: OdooRollbackAttempt;
  dependencies: OdooRollbackDependencies;
  onOperation: (operation: OdooProdRollbackOperationView) => void;
  onQueued?: (operationId: string) => void;
  pollIntervalMilliseconds?: number;
  scope: OdooReleaseScope;
  signal?: AbortSignal;
}): Promise<OdooRollbackOutcome> {
  const rollbackScope = { context: scope.context, product: scope.product };
  let operation: OdooProdRollbackOperationView | null = null;
  try {
    let current = attempt.operationId
      ? await dependencies.readRollback(attempt.operationId, rollbackScope, signal)
      : await dependencies.enqueueRollback(
          {
            product: scope.product,
            rollback: {
              context: scope.context,
              instance: scope.environment,
              reason: attempt.reason,
              schema_version: 1,
            },
            schema_version: 1,
          },
          { idempotencyKey: attempt.idempotencyKey, signal },
        );
    operation = current.operation;
    if (!attempt.operationId) {
      onQueued?.(operation.operation_id);
    }
    onOperation(operation);
    while (RELEASE_ACTIVE_STATUSES.has(current.operation.status)) {
      await dependencies.wait(pollIntervalMilliseconds, signal);
      current = await dependencies.readRollback(operation.operation_id, rollbackScope, signal);
      operation = current.operation;
      onOperation(operation);
    }
    if (operation.status === "pass") {
      return { operation, status: "rolled_back" };
    }
    return {
      certainty: "definitive",
      failure: releaseStopFailure(operation, current.trace_id, "rollback"),
      operation,
      status: "stopped",
    };
  } catch (error) {
    return {
      certainty: odooReleaseFailureCertainty(error),
      failure: odooReleaseFailure(error),
      operation,
      status: "stopped",
    };
  }
}

export function readOdooRollbackAttempt(
  scope: OdooReleaseScope,
  storage: BrowserOperationStorage | null = sessionStorageOrNull(),
): OdooRollbackAttempt | null {
  const candidate = readStoredJson(rollbackStorageKey(scope), storage) as
    | Partial<OdooRollbackAttempt>
    | null;
  if (
    !candidate ||
    typeof candidate.idempotencyKey !== "string" ||
    !candidate.idempotencyKey ||
    typeof candidate.reason !== "string"
  ) {
    return null;
  }
  return {
    idempotencyKey: candidate.idempotencyKey,
    reason: candidate.reason,
    ...(typeof candidate.operationId === "string" && candidate.operationId
      ? { operationId: candidate.operationId }
      : {}),
  };
}

export function writeOdooRollbackAttempt(
  scope: OdooReleaseScope,
  attempt: OdooRollbackAttempt | null,
  storage: BrowserOperationStorage | null = sessionStorageOrNull(),
): void {
  writeStoredJson(rollbackStorageKey(scope), attempt, storage);
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
  const candidate = readStoredJson(attemptStorageKey(scope), storage) as
    | Partial<OdooReleaseAttempt>
    | null;
  if (
    !candidate ||
    typeof candidate.idempotencyKey !== "string" ||
    !candidate.idempotencyKey ||
    typeof candidate.requestId !== "string" ||
    !candidate.requestId
  ) {
    return null;
  }
  return {
    idempotencyKey: candidate.idempotencyKey,
    requestId: candidate.requestId,
    ...(typeof candidate.promotionOperationId === "string" && candidate.promotionOperationId
      ? { promotionOperationId: candidate.promotionOperationId }
      : {}),
  };
}

export function writeOdooReleaseAttempt(
  scope: OdooReleaseScope,
  attempt: OdooReleaseAttempt | null,
  storage: BrowserOperationStorage | null = sessionStorageOrNull(),
): void {
  writeStoredJson(attemptStorageKey(scope), attempt, storage);
}

function readStoredJson(key: string, storage: BrowserOperationStorage | null): unknown {
  try {
    const value = storage?.getItem(key);
    return value ? (JSON.parse(value) as unknown) : null;
  } catch {
    return null;
  }
}

function writeStoredJson(
  key: string,
  value: object | null,
  storage: BrowserOperationStorage | null,
): void {
  try {
    if (value) {
      storage?.setItem(key, JSON.stringify(value));
    } else {
      storage?.removeItem(key);
    }
  } catch {
    return;
  }
}

function attemptStorageKey(scope: OdooReleaseScope): string {
  return `launchplane.odoo-release.${scope.product.trim()}.${scope.environment.trim()}`;
}

function rollbackStorageKey(scope: OdooReleaseScope): string {
  return `launchplane.odoo-rollback.${scope.product.trim()}.${scope.environment.trim()}`;
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
