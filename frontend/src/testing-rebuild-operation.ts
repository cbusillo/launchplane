import { LaunchplaneApiError } from "./api";
import type { BrowserOperationOptions, BrowserOperationStorage } from "./browser-operation";
import type { SubmitTestingRebuildData, TestingRebuildResponse } from "./generated/openapi.ts";

export interface TestingRebuildAttempt {
  product: string;
  key: string;
  payload: SubmitTestingRebuildData["body"];
  operationId?: string;
  denied?: boolean;
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
      && (attempt.operationId === undefined || typeof attempt.operationId === "string" && !!attempt.operationId.trim())
      && (attempt.denied === undefined || typeof attempt.denied === "boolean")) return attempt;
  } catch { /* Unreadable browser state is not an operation handle. */ }
  return null;
}

interface TestingRebuildDependencies {
  submit: (product: string, payload: SubmitTestingRebuildData["body"], options: BrowserOperationOptions) => Promise<TestingRebuildResponse>;
  read: (product: string, operationId: string, signal?: AbortSignal) => Promise<TestingRebuildResponse>;
  wait: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
  persist: (attempt: TestingRebuildAttempt | null) => void;
  progress: (response: TestingRebuildResponse) => void;
}

export async function runTestingRebuild(attempt: TestingRebuildAttempt, dependencies: TestingRebuildDependencies, signal?: AbortSignal): Promise<TestingRebuildResponse> {
  if (attempt.denied) throw new Error("This request was denied. It will not be submitted again.");
  dependencies.persist(attempt);
  let response: TestingRebuildResponse;
  try {
    response = attempt.operationId
      ? await dependencies.read(attempt.product, attempt.operationId, signal)
      : await dependencies.submit(attempt.product, attempt.payload, { idempotencyKey: attempt.key, signal });
  } catch (error) {
    if (error instanceof LaunchplaneApiError && [401, 403].includes(error.statusCode)) {
      dependencies.persist({ ...attempt, denied: true });
    } else if (!attempt.operationId && error instanceof LaunchplaneApiError && error.statusCode === 409
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
    attempt = { ...attempt, operationId: response.operation.operation_id };
    dependencies.persist(attempt);
    dependencies.progress(response);
    if (!["pending", "running"].includes(response.operation.status)) return response;
    await dependencies.wait(2500, signal);
    response = await dependencies.read(attempt.product, attempt.operationId!, signal);
  }
}
