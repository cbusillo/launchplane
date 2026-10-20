import { LaunchplaneApiError } from "./api";
import type { BrowserOperationOptions, BrowserOperationStorage } from "./browser-operation";
import type { SubmitTestingRebuildData, TestingRebuildResponse, TestingRebuildOperationView, ResumeTestingRebuildData } from "./generated/openapi.ts";
import type { SettleTestingRebuildData, TestingRebuildSettlementResponse } from "./generated/openapi.ts";

export interface TestingRebuildAttempt {
  product: string;
  key: string;
  payload: SubmitTestingRebuildData["body"];
  operationId?: string;
  denied?: boolean;
  dispatchStarted?: boolean;
  readOnlyRecovery?: boolean;
  resumeNeedsRead?: boolean;
}

export function saveTestingRebuildAttempt(product: string, attempt: TestingRebuildAttempt | null, storage: BrowserOperationStorage = sessionStorage) {
  const key = `launchplane:testing-rebuild:${product}`;
  try {
    if (attempt) storage.setItem(key, JSON.stringify(attempt));
    else storage.removeItem(key);
  } catch { /* The server still retains the operation and original key. */ }
}

export function readTestingRebuildAttempt(product: string, storage: BrowserOperationStorage = sessionStorage): TestingRebuildAttempt | null {
  try {
    const attempt = JSON.parse(storage.getItem(`launchplane:testing-rebuild:${product}`) ?? "null") as TestingRebuildAttempt | null;
    if (attempt?.product === product && typeof attempt.key === "string" && attempt.key.trim()
      && attempt.key.length <= 256 && typeof attempt.payload?.artifact_id === "string" && attempt.payload.artifact_id
      && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(attempt.payload.source_git_ref)
      && ["empty", "production_restore", "upstream_restore"].includes(attempt.payload.data_source_mode)
      && typeof attempt.payload.reason === "string" && attempt.payload.reason.trim() && attempt.payload.reason.length <= 1000
      && (attempt.payload.original_scope === undefined || typeof attempt.payload.original_scope === "string")
      && (attempt.operationId === undefined || typeof attempt.operationId === "string" && !!attempt.operationId.trim())
      && (attempt.denied === undefined || typeof attempt.denied === "boolean")
      && (attempt.dispatchStarted === undefined || typeof attempt.dispatchStarted === "boolean")
      && (attempt.readOnlyRecovery === undefined || typeof attempt.readOnlyRecovery === "boolean")
      && (attempt.resumeNeedsRead === undefined || typeof attempt.resumeNeedsRead === "boolean")) {
      // Older saved denials did not distinguish a write refusal from a failed
      // recovery/read. Preserve their key and allow only lookup, never a write.
      if (!attempt.payload.original_scope?.trim()) {
        return { ...attempt, dispatchStarted: true, denied: false, readOnlyRecovery: true };
      }
      return attempt.dispatchStarted === undefined
        ? { ...attempt, dispatchStarted: true, denied: false, readOnlyRecovery: !!attempt.denied }
        : attempt;
    }
  } catch { /* Unreadable browser state is not an operation handle. */ }
  return null;
}

export async function settleTestingRebuildAttempt(
  attempt: TestingRebuildAttempt,
  dependencies: {
    settle: (product: string, payload: SettleTestingRebuildData["body"], options: BrowserOperationOptions) => Promise<TestingRebuildSettlementResponse>;
    persist: (attempt: TestingRebuildAttempt | null) => void;
  },
  signal?: AbortSignal,
): Promise<TestingRebuildSettlementResponse> {
  const scope = attempt.payload.original_scope;
  if (!scope?.trim() || attempt.operationId) {
    throw new Error("This saved request needs observation of its original operation.");
  }
  const retained = { ...attempt, denied: false, readOnlyRecovery: true };
  dependencies.persist(retained);
  const settled = await dependencies.settle(attempt.product, { ...attempt.payload, original_scope: scope }, {
    idempotencyKey: attempt.key, signal,
  });
  const intent = settled.original_request;
  if (settled.product !== attempt.product || settled.original_key !== attempt.key
    || intent.original_scope !== scope || intent.data_source_mode !== attempt.payload.data_source_mode
    || intent.artifact_id !== attempt.payload.artifact_id || intent.source_git_ref !== attempt.payload.source_git_ref
    || intent.reason !== attempt.payload.reason) {
    throw new Error("The settlement does not identify the original request. Keep its recovery state.");
  }
  if (settled.outcome === "excluded" && settled.operation === null) {
    dependencies.persist(null);
  } else if (settled.outcome === "observed" && settled.operation?.product === attempt.product
    && settled.operation.instance === "testing" && settled.operation.operation_id) {
    dependencies.persist(["pass", "fail", "cancelled", "superseded"].includes(settled.operation.status)
      ? null : { ...retained, operationId: settled.operation.operation_id });
  } else {
    throw new Error("The settlement outcome is unknown. Keep the original request.");
  }
  return settled;
}

interface TestingRebuildDependencies {
  submit: (product: string, payload: SubmitTestingRebuildData["body"], options: BrowserOperationOptions) => Promise<TestingRebuildResponse>;
  read: (product: string, operationId: string, signal?: AbortSignal) => Promise<TestingRebuildResponse>;
  lookup: (product: string, payload: SubmitTestingRebuildData["body"], options: BrowserOperationOptions) => Promise<TestingRebuildResponse>;
  wait: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
  persist: (attempt: TestingRebuildAttempt | null) => void;
  progress: (response: TestingRebuildResponse) => void;
}

export async function runTestingRebuild(attempt: TestingRebuildAttempt, dependencies: TestingRebuildDependencies, signal?: AbortSignal): Promise<TestingRebuildResponse> {
  if (attempt.denied) throw new Error("This request was denied. It will not be submitted again.");
  const previouslyDispatched = !!attempt.dispatchStarted;
  const readingOriginal = !!attempt.operationId;
  attempt = { ...attempt, dispatchStarted: previouslyDispatched };
  dependencies.persist(attempt);
  const onDispatch = () => {
    attempt = { ...attempt, dispatchStarted: true };
    dependencies.persist(attempt);
  };
  let response: TestingRebuildResponse;
  try {
    response = attempt.operationId
      ? await dependencies.read(attempt.product, attempt.operationId, signal)
      : attempt.readOnlyRecovery
        ? await dependencies.lookup(attempt.product, attempt.payload, { idempotencyKey: attempt.key, signal })
        : await dependencies.submit(attempt.product, attempt.payload, { idempotencyKey: attempt.key, signal, onDispatch });
  } catch (error) {
    if (!attempt.operationId && !previouslyDispatched && !attempt.dispatchStarted) {
      dependencies.persist(null);
    } else if (error instanceof LaunchplaneApiError && [401, 403].includes(error.statusCode)) {
      dependencies.persist(attempt.operationId || previouslyDispatched
        ? { ...attempt, readOnlyRecovery: true }
        : { ...attempt, denied: true });
    } else if (!attempt.operationId && !previouslyDispatched && error instanceof LaunchplaneApiError && error.statusCode === 409
      && ["testing_rebuild_not_ready", "testing_staff_hold", "testing_in_use", "testing_needed_copy",
        "testing_production_resources_shared", "testing_credentials_unverified", "testing_restore_source_unconfigured"].includes(error.code)) {
      // These are explicit admission refusals before an operation is queued.
      // Keep uncertainty and key collisions; only a fresh inspection can start anew.
      dependencies.persist(null);
    }
    throw error;
  }
  while (true) {
    if (response.operation.product !== attempt.product || response.operation.instance !== "testing"
      || !response.operation.operation_id || attempt.operationId && response.operation.operation_id !== attempt.operationId) {
      throw new Error("The response does not identify this testing rebuild. Keep the original request for inspection.");
    }
    attempt = { ...attempt, operationId: response.operation.operation_id, ...(readingOriginal ? { resumeNeedsRead: false } : {}) };
    dependencies.persist(attempt);
    dependencies.progress(response);
    if (!["pending", "running"].includes(response.operation.status)) return response;
    await dependencies.wait(2500, signal);
    response = await dependencies.read(attempt.product, attempt.operationId!, signal);
  }
}

export async function resumeTestingRebuildAttempt(
  attempt: TestingRebuildAttempt,
  operation: TestingRebuildOperationView,
  dependencies: {
    resume: (product: string, operationId: string, payload: ResumeTestingRebuildData["body"], options: BrowserOperationOptions) => Promise<TestingRebuildResponse>;
    persist: (attempt: TestingRebuildAttempt) => void;
  },
  signal?: AbortSignal,
): Promise<TestingRebuildResponse> {
  if (!attempt.operationId || attempt.denied || attempt.resumeNeedsRead || operation.operation_id !== attempt.operationId
    || operation.product !== attempt.product || operation.instance !== "testing" || operation.status !== "reconciliation_required") {
    throw new Error("Read the original held rebuild before resuming it.");
  }
  try {
    const response = await dependencies.resume(attempt.product, attempt.operationId, {
      expected_updated_at: operation.updated_at, expected_attempt: operation.attempt,
    }, { idempotencyKey: attempt.key, signal });
    if (response.operation.operation_id !== attempt.operationId || response.operation.product !== attempt.product || response.operation.instance !== "testing") {
      throw new Error("The resume response does not identify the original rebuild.");
    }
    return response;
  } catch (error) {
    dependencies.persist({ ...attempt, resumeNeedsRead: true });
    throw error;
  }
}
