export type BrowserActionKind =
  | "inspect"
  | "dry-run"
  | "apply"
  | "workflow-dispatch"
  | "destructive"
  | "unsupported";

export interface BrowserOperationIdentity {
  idempotencyKey: string;
  requestFingerprint: string;
}

export interface BrowserOperationOptions {
  idempotencyKey: string;
  onDispatch?: () => void;
  signal?: AbortSignal;
}

export interface BrowserOperationReceipt {
  traceId: string;
  originalTraceId: string;
  replayed: boolean;
}

export type BrowserOperationPhase =
  | "idle"
  | "ready"
  | "queued"
  | "submitting"
  | "succeeded"
  | "failed"
  | "uncertain"
  | "cancelled";

export interface BrowserOperationFailure {
  code: string;
  message: string;
  statusCode: number;
  traceId: string;
}

export interface BrowserOperationState {
  failure: BrowserOperationFailure | null;
  identity: BrowserOperationIdentity | null;
  phase: BrowserOperationPhase;
  receipt: BrowserOperationReceipt | null;
  requiresIdempotencyContinuity: boolean;
}

export interface BrowserOperationStorage {
  getItem: (key: string) => string | null;
  removeItem: (key: string) => void;
  setItem: (key: string, value: string) => void;
}

export interface BrowserOperationEnvelope {
  original_trace_id?: string | null;
  replayed?: boolean | null;
  trace_id: string;
}

export function createBrowserOperationState(): BrowserOperationState {
  return {
    failure: null,
    identity: null,
    phase: "idle",
    receipt: null,
    requiresIdempotencyContinuity: false,
  };
}

export function recoverBrowserOperationState(
  scope: string,
  storage: BrowserOperationStorage | null = browserOperationSessionStorage(),
): BrowserOperationState {
  if (!storage) {
    return createBrowserOperationState();
  }
  const key = browserOperationStorageKey(scope);
  try {
    const value = storage.getItem(key);
    if (!value) {
      return createBrowserOperationState();
    }
    const candidate = JSON.parse(value) as Partial<BrowserOperationState>;
    const identity = candidate.identity;
    if (
      !identity ||
      typeof identity.idempotencyKey !== "string" ||
      !identity.idempotencyKey ||
      typeof identity.requestFingerprint !== "string" ||
      !identity.requestFingerprint
    ) {
      storage.removeItem(key);
      return createBrowserOperationState();
    }
    if (candidate.phase !== "submitting" && candidate.phase !== "uncertain") {
      storage.removeItem(key);
      return createBrowserOperationState();
    }
    return {
      failure:
        candidate.phase === "uncertain" && candidate.failure
          ? candidate.failure
          : {
              code: "request_interrupted",
              message:
                "The browser stopped before the dispatched operation settled. Retry only with the existing idempotency key.",
              statusCode: 0,
              traceId: "",
            },
      identity,
      phase: "uncertain",
      receipt: null,
      requiresIdempotencyContinuity: true,
    };
  } catch {
    storage.removeItem(key);
    return createBrowserOperationState();
  }
}

export function persistBrowserOperationState(
  scope: string,
  state: BrowserOperationState,
  storage: BrowserOperationStorage | null = browserOperationSessionStorage(),
): void {
  if (!storage) {
    return;
  }
  const key = browserOperationStorageKey(scope);
  try {
    if (
      state.identity &&
      (state.phase === "submitting" || state.requiresIdempotencyContinuity)
    ) {
      storage.setItem(key, JSON.stringify(state));
    } else {
      storage.removeItem(key);
    }
  } catch {
    return;
  }
}

function browserOperationStorageKey(scope: string): string {
  const normalizedScope = scope.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return `launchplane.browser-operation.${normalizedScope || "operation"}`;
}

export async function browserOperationIdentity(
  scope: string,
  request: unknown,
  current?: BrowserOperationIdentity | null,
): Promise<BrowserOperationIdentity> {
  const requestFingerprint = await stableRequestFingerprint({ request, scope });
  if (current?.requestFingerprint === requestFingerprint) {
    return current;
  }
  return {
    idempotencyKey: createBrowserIdempotencyKey(scope),
    requestFingerprint,
  };
}

export async function prepareBrowserOperation(
  scope: string,
  request: unknown,
  current: BrowserOperationState = createBrowserOperationState(),
): Promise<BrowserOperationState> {
  const identity = await browserOperationIdentity(scope, request, current.identity);
  const requestChanged =
    current.identity !== null &&
    identity.requestFingerprint !== current.identity.requestFingerprint;
  if (requestChanged && ["queued", "submitting"].includes(current.phase)) {
    throw new Error("Cannot replace a browser operation while it is submitting.");
  }
  if (requestChanged && current.requiresIdempotencyContinuity) {
    throw new Error(
      "Cannot create a new browser operation while the previous result is uncertain. Retry with the existing idempotency key.",
    );
  }
  if (!requestChanged && current.identity !== null) {
    return current;
  }
  return {
    failure: null,
    identity,
    phase: "ready",
    receipt: null,
    requiresIdempotencyContinuity: false,
  };
}

export function beginBrowserOperation(
  current: BrowserOperationState,
): BrowserOperationState {
  if (current.phase !== "ready" || current.identity === null) {
    throw new Error("Browser operation must be ready before it can submit.");
  }
  return {
    ...current,
    failure: null,
    phase: "queued",
    receipt: null,
  };
}

export function markBrowserOperationDispatched(
  current: BrowserOperationState,
): BrowserOperationState {
  if (current.phase !== "queued" || current.identity === null) {
    throw new Error("Browser operation must be queued before it can dispatch.");
  }
  return {
    ...current,
    phase: "submitting",
  };
}

export function completeBrowserOperation(
  current: BrowserOperationState,
  envelope: BrowserOperationEnvelope,
): BrowserOperationState {
  requireSubmittingOperation(current);
  return {
    ...current,
    failure: null,
    phase: "succeeded",
    receipt: browserOperationReceipt(envelope),
    requiresIdempotencyContinuity: false,
  };
}

export function failBrowserOperation(
  current: BrowserOperationState,
  failure: BrowserOperationFailure,
  certainty: "definitive" | "uncertain",
): BrowserOperationState {
  requireActiveOperation(current);
  const remainsUncertain =
    certainty === "uncertain" || current.requiresIdempotencyContinuity;
  return {
    ...current,
    failure,
    phase: remainsUncertain ? "uncertain" : "failed",
    receipt: null,
    requiresIdempotencyContinuity: remainsUncertain,
  };
}

export function cancelBrowserOperation(
  current: BrowserOperationState,
): BrowserOperationState {
  if (current.phase === "ready" || current.phase === "queued") {
    if (current.requiresIdempotencyContinuity) {
      return {
        ...current,
        failure: {
          code: "request_cancelled",
          message:
            "The retry was cancelled before dispatch, but the original result is still uncertain. Retry only with the existing idempotency key.",
          statusCode: 0,
          traceId: "",
        },
        phase: "uncertain",
        receipt: null,
      };
    }
    return {
      ...current,
      failure: null,
      phase: "cancelled",
      receipt: null,
    };
  }
  if (current.phase === "submitting") {
    return {
      ...current,
      failure: {
        code: "request_cancelled",
        message:
          "The browser stopped waiting after dispatch. The result is uncertain; retry only with the existing idempotency key.",
        statusCode: 0,
        traceId: "",
      },
      phase: "uncertain",
      receipt: null,
      requiresIdempotencyContinuity: true,
    };
  }
  return current;
}

export function retryBrowserOperation(
  current: BrowserOperationState,
): BrowserOperationState {
  if (
    current.identity === null ||
    !["failed", "uncertain", "cancelled"].includes(current.phase)
  ) {
    throw new Error("Only a failed, uncertain, or cancelled browser operation can retry.");
  }
  return {
    ...current,
    failure: null,
    phase: "ready",
    receipt: null,
  };
}

export function resetBrowserOperation(
  current: BrowserOperationState,
): BrowserOperationState {
  if (
    current.phase === "queued" ||
    current.phase === "submitting" ||
    current.requiresIdempotencyContinuity
  ) {
    throw new Error(
      "Cannot discard an in-flight or uncertain browser operation idempotency key.",
    );
  }
  return createBrowserOperationState();
}

function browserOperationReceipt(
  envelope: BrowserOperationEnvelope,
): BrowserOperationReceipt {
  return {
    traceId: envelope.trace_id,
    originalTraceId: envelope.original_trace_id ?? "",
    replayed: envelope.replayed === true,
  };
}

function createBrowserIdempotencyKey(scope: string): string {
  const normalizedScope = scope
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  const operationId = globalThis.crypto.randomUUID();
  return `ui-${normalizedScope || "operation"}-${operationId}`;
}

export async function stableRequestFingerprint(value: unknown): Promise<string> {
  const encoded = new TextEncoder().encode(
    JSON.stringify(normalizeRequestValue(value)) ?? "undefined",
  );
  const digest = await globalThis.crypto.subtle.digest("SHA-256", encoded);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function requireSubmittingOperation(current: BrowserOperationState): void {
  if (current.phase !== "submitting" || current.identity === null) {
    throw new Error("Browser operation must be submitting before it can settle.");
  }
}

function requireActiveOperation(current: BrowserOperationState): void {
  if (
    !["queued", "submitting"].includes(current.phase) ||
    current.identity === null
  ) {
    throw new Error("Browser operation must be active before it can fail.");
  }
}

function normalizeRequestValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(normalizeRequestValue);
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, nestedValue]) => [key, normalizeRequestValue(nestedValue)]);
    return Object.fromEntries(entries);
  }
  return value;
}

function browserOperationSessionStorage(): BrowserOperationStorage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
