import type {
  ApiErrorPayload,
  AuthSessionPayload,
  LogoutPayload,
  ProductListPayload,
} from "./types";
import type {
  ApplyProductEnvironmentConfigData,
  ApplyProductEnvironmentConfigResponse,
  ApproveHumanPrivilegedOperationData,
  ApproveHumanPrivilegedOperationResponse,
  DispatchProductPromotionWorkflowData,
  DispatchProductPromotionWorkflowResponse,
  DryRunProductPromotionData,
  DryRunProductPromotionResponse,
  EveryCodeSummaryResponse,
  EvaluateOwnerAcceptanceResponse,
  GovernanceProjectionResponse,
  ListHumanPrivilegedOperationsResponse,
  ListOwnerAcceptanceCurrentItemsData,
  ListOwnerAcceptanceQueueData,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
  OwnerAcceptanceQueueResponse,
  OwnerAcceptanceCurrentItemsResponse,
  OwnerAcceptanceDecision,
  OwnerAcceptanceEventResponse,
  OwnerAcceptanceProductDecision,
  ProductActivityResponse,
  ProductEnvironmentConfigStatusResponse,
  ProductEnvironmentIncidentResponse,
  ProductEnvironmentIncidentsResponse,
  ProductOperationalReadinessResponse,
  ProductEnvironmentResponse,
  ProductOverviewResponse,
  ProductPromotionStatusResponse,
  ProductPromotionWorkflowDeliveryStatusResponse,
  PrivilegedOperationHumanResponse,
  PrivilegedOperationListResponse,
  PrivilegedOperationRecord,
  RankWorkGraphSnapshotData,
  RankWorkGraphSnapshotResponse,
  ReadProductOperationalReadinessData,
  ReadTenantAdmissionEvaluationData,
  RevokeHumanPrivilegedOperationData,
  RevokeHumanPrivilegedOperationResponse,
  TenantAdmissionEvaluationReadResponse,
  WorkGraphIssueInboxResponse,
  WorkGraphSnapshot,
  WorkGraphSnapshotResponse,
  WriteOwnerAcceptanceEventData,
} from "./generated/openapi.ts";
import type { BrowserOperationOptions } from "./browser-operation";
import {
  BROWSER_WRITE_ROUTES,
  type BrowserWriteRoute,
} from "./browser-write-contract";

export class LaunchplaneApiError extends Error {
  code: string;
  statusCode: number;
  traceId: string;

  constructor(message: string, statusCode: number, traceId = "", code = "") {
    super(message);
    this.name = "LaunchplaneApiError";
    this.code = code;
    this.statusCode = statusCode;
    this.traceId = traceId;
  }
}

let browserMutationQueue: Promise<void> = Promise.resolve();

async function requestJson<T>(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
  signal?: AbortSignal,
  idempotencyKey = "",
  onDispatch?: () => void,
): Promise<T> {
  if (method === "GET") {
    return performJsonRequest<T>(path, method, body, signal);
  }
  const queuedRequest = browserMutationQueue.then(async () => {
    const session = await performJsonRequest<AuthSessionPayload>(
      "/v1/auth/session",
      "GET",
      undefined,
      signal,
    );
    onDispatch?.();
    return performJsonRequest<T>(
      path,
      method,
      body,
      signal,
      session.csrf_token,
      idempotencyKey,
    );
  });
  browserMutationQueue = queuedRequest.then(
    () => undefined,
    () => undefined,
  );
  return queuedRequest;
}

async function performJsonRequest<T>(
  path: string,
  method: "GET" | "POST",
  body?: unknown,
  signal?: AbortSignal,
  csrfToken = "",
  idempotencyKey = "",
): Promise<T> {
  const headers: HeadersInit = {
    Accept: "application/json",
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const payload = (await response.json()) as T | ApiErrorPayload;
  if (!response.ok) {
    const errorPayload = payload as ApiErrorPayload;
    const errorMessage =
      "error" in errorPayload && errorPayload.error
        ? (errorPayload.error.message ??
          `Launchplane API returned ${response.status}.`)
        : `Launchplane API returned ${response.status}.`;
    const traceId =
      "trace_id" in errorPayload && typeof errorPayload.trace_id === "string"
        ? errorPayload.trace_id
        : "";
    const errorCode =
      "error" in errorPayload && errorPayload.error
        ? (errorPayload.error.code ?? "")
        : "";
    throw new LaunchplaneApiError(
      errorMessage,
      response.status,
      traceId,
      errorCode,
    );
  }
  return payload as T;
}

function requestGeneratedPost<
  T,
  TRequest extends {
    body: unknown;
    headers?: object;
    url: BrowserWriteRoute;
  } = {
    body: unknown;
    headers?: object;
    url: BrowserWriteRoute;
  },
>(
  request: TRequest,
  signal?: AbortSignal,
  onDispatch?: () => void,
): Promise<T> {
  return requestJson<T>(
    request.url,
    "POST",
    request.body,
    signal,
    generatedIdempotencyKey(request.headers),
    onDispatch,
  );
}

function generatedIdempotencyKey(headers?: object): string {
  if (!headers) {
    return "";
  }
  const value = (headers as Record<string, unknown>)["Idempotency-Key"];
  return typeof value === "string" ? value.trim() : "";
}

export function readAuthSession(
  signal?: AbortSignal,
): Promise<AuthSessionPayload> {
  return requestJson<AuthSessionPayload>(
    "/v1/auth/session",
    "GET",
    undefined,
    signal,
  );
}

export function logout(): Promise<LogoutPayload> {
  return requestJson<LogoutPayload>("/auth/logout", "POST");
}

export function listProducts(
  signal?: AbortSignal,
): Promise<ProductListPayload> {
  return requestJson<ProductListPayload>(
    "/v1/products",
    "GET",
    undefined,
    signal,
  );
}

export function readGovernanceProjection(
  repository: string,
  pullRequestNumber: number,
  baseBranch = "main",
  signal?: AbortSignal,
): Promise<GovernanceProjectionResponse> {
  const params = new URLSearchParams({
    repository,
    pull_request_number: String(pullRequestNumber),
    base_branch: baseBranch,
  });
  return requestJson<GovernanceProjectionResponse>(
    `/v1/governance/projection?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProduct(
  product: string,
  signal?: AbortSignal,
): Promise<ProductOverviewResponse> {
  return requestJson<ProductOverviewResponse>(
    `/v1/products/${encodeURIComponent(product)}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductActivity(
  product: string,
  signal?: AbortSignal,
): Promise<ProductActivityResponse> {
  return requestJson<ProductActivityResponse>(
    `/v1/products/${encodeURIComponent(product)}/activity`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductEnvironment(
  product: string,
  environment: string,
  signal?: AbortSignal,
): Promise<ProductEnvironmentResponse> {
  return requestJson<ProductEnvironmentResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}`,
    "GET",
    undefined,
    signal,
  );
}

export function listProductEnvironmentIncidents(
  product: string,
  environment: string,
  signal?: AbortSignal,
): Promise<ProductEnvironmentIncidentsResponse> {
  return requestJson<ProductEnvironmentIncidentsResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/public-ingress/incidents`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductEnvironmentIncident(
  product: string,
  environment: string,
  incidentId: string,
  signal?: AbortSignal,
): Promise<ProductEnvironmentIncidentResponse> {
  return requestJson<ProductEnvironmentIncidentResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/public-ingress/incidents/${encodeURIComponent(incidentId)}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductOperationalReadiness(
  request: Pick<ReadProductOperationalReadinessData, "path" | "query">,
  signal?: AbortSignal,
): Promise<ProductOperationalReadinessResponse> {
  const params = new URLSearchParams({ action: request.query.action });
  if (request.query.artifact_id) {
    params.set("artifact_id", request.query.artifact_id);
  }
  if (request.query.expected_current_artifact_id) {
    params.set(
      "expected_current_artifact_id",
      request.query.expected_current_artifact_id,
    );
  }
  return requestJson<ProductOperationalReadinessResponse>(
    `/v1/products/${encodeURIComponent(request.path.product)}/contexts/${encodeURIComponent(request.path.context)}/instances/${encodeURIComponent(request.path.instance)}/operational-readiness?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductEnvironmentConfigStatus(
  product: string,
  environment: string,
  signal?: AbortSignal,
): Promise<ProductEnvironmentConfigStatusResponse> {
  return requestJson<ProductEnvironmentConfigStatusResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/config-status`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductPromotionStatus(
  product: string,
  environment: string,
  signal?: AbortSignal,
): Promise<ProductPromotionStatusResponse> {
  return requestJson<ProductPromotionStatusResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/promotion-status`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductPromotionWorkflowDelivery(
  product: string,
  environment: string,
  deliveryId: string,
  signal?: AbortSignal,
): Promise<ProductPromotionWorkflowDeliveryStatusResponse> {
  return requestJson<ProductPromotionWorkflowDeliveryStatusResponse>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/promotion/workflow-deliveries/${encodeURIComponent(deliveryId)}`,
    "GET",
    undefined,
    signal,
  );
}

export function readEveryCodeSummary(
  limit = 12,
  signal?: AbortSignal,
): Promise<EveryCodeSummaryResponse> {
  return requestJson<EveryCodeSummaryResponse>(
    `/v1/every-code/summary?limit=${encodeURIComponent(String(limit))}`,
    "GET",
    undefined,
    signal,
  );
}

export function readWorkGraphSnapshot(
  signal?: AbortSignal,
): Promise<WorkGraphSnapshotResponse> {
  return requestJson<WorkGraphSnapshotResponse>(
    "/v1/work-graph/snapshot",
    "GET",
    undefined,
    signal,
  );
}

export function readTenantAdmissionEvaluation(
  request: Pick<ReadTenantAdmissionEvaluationData, "query">,
  signal?: AbortSignal,
): Promise<TenantAdmissionEvaluationReadResponse> {
  const params = new URLSearchParams({
    product: request.query.product,
    context: request.query.context,
    repository_id: request.query.repository_id,
    repository_owner_id: request.query.repository_owner_id,
    repository: request.query.repository,
    pull_request_number: String(request.query.pull_request_number),
    head_sha: request.query.head_sha,
    base_branch: request.query.base_branch,
    merge_method: request.query.merge_method ?? "merge",
  });
  return requestJson<TenantAdmissionEvaluationReadResponse>(
    `/v1/work-graph/tenant-admission/evaluation?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function rankWorkGraphSnapshot(
  snapshot: WorkGraphSnapshot,
  limit = 12,
  signal?: AbortSignal,
): Promise<RankWorkGraphSnapshotResponse> {
  const request: RankWorkGraphSnapshotData = {
    url: BROWSER_WRITE_ROUTES.workGraphRank,
    body: {
      snapshot,
      limit,
    },
  };
  return requestGeneratedPost<RankWorkGraphSnapshotResponse>(request, signal);
}

export function readGitHubIssueInbox(
  signal?: AbortSignal,
): Promise<WorkGraphIssueInboxResponse> {
  return requestJson<WorkGraphIssueInboxResponse>(
    "/v1/work-graph/github/issues",
    "GET",
    undefined,
    signal,
  );
}

export function readMergeTrainControllerStatus(
  repository: string,
  baseBranch: string,
  signal?: AbortSignal,
): Promise<MergeTrainControllerStatusResponse> {
  const params = new URLSearchParams({
    repository,
    base_branch: baseBranch,
  });
  return requestJson<MergeTrainControllerStatusResponse>(
    `/v1/work-graph/merge-train/controller/status?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function readMergeTrainPolicyTargets(
  signal?: AbortSignal,
): Promise<MergeTrainPolicyTargetsResponse> {
  return requestJson<MergeTrainPolicyTargetsResponse>(
    "/v1/work-graph/merge-train/policy-targets",
    "GET",
    undefined,
    signal,
  );
}

export function applyProductEnvironmentConfig(
  product: string,
  environment: string,
  payload: ApplyProductEnvironmentConfigData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductEnvironmentConfigResponse> {
  const request: ApplyProductEnvironmentConfigData = {
    url: BROWSER_WRITE_ROUTES.productEnvironmentConfigApply,
    path: { product, environment },
    body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson<ApplyProductEnvironmentConfigResponse>(
    `/v1/products/${encodeURIComponent(request.path.product)}/environments/${encodeURIComponent(request.path.environment)}/config/apply`,
    "POST",
    request.body,
    options.signal,
    generatedIdempotencyKey(request.headers),
    options.onDispatch,
  );
}

export function dryRunProductPromotion(
  product: string,
  environment: string,
  payload: DryRunProductPromotionData["body"],
  options: BrowserOperationOptions,
): Promise<DryRunProductPromotionResponse> {
  const request: DryRunProductPromotionData = {
    url: BROWSER_WRITE_ROUTES.productPromotionDryRun,
    path: { product, environment },
    body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson<DryRunProductPromotionResponse>(
    `/v1/products/${encodeURIComponent(request.path.product)}/environments/${encodeURIComponent(request.path.environment)}/promotion/dry-run`,
    "POST",
    request.body,
    options.signal,
    generatedIdempotencyKey(request.headers),
    options.onDispatch,
  );
}

export function dispatchProductPromotionWorkflow(
  product: string,
  environment: string,
  payload: DispatchProductPromotionWorkflowData["body"],
  options: BrowserOperationOptions,
): Promise<DispatchProductPromotionWorkflowResponse> {
  const request: DispatchProductPromotionWorkflowData = {
    url: BROWSER_WRITE_ROUTES.productPromotionWorkflowDispatch,
    path: { product, environment },
    body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson<DispatchProductPromotionWorkflowResponse>(
    `/v1/products/${encodeURIComponent(request.path.product)}/environments/${encodeURIComponent(request.path.environment)}/promotion/workflow-dispatch`,
    "POST",
    request.body,
    options.signal,
    generatedIdempotencyKey(request.headers),
    options.onDispatch,
  );
}

export function readOwnerAcceptanceQueue(
  query: ListOwnerAcceptanceQueueData["query"] = {},
  signal?: AbortSignal,
): Promise<OwnerAcceptanceQueueResponse> {
  const params = new URLSearchParams();
  if (query.repository) params.set("repository", query.repository);
  if (query.status) params.set("status", query.status);
  const qs = params.toString();
  return requestJson<OwnerAcceptanceQueueResponse>(
    `/v1/owner-acceptance/queue${qs ? `?${qs}` : ""}`,
    "GET",
    undefined,
    signal,
  );
}

export function readOwnerAcceptanceCurrentItems(
  query: ListOwnerAcceptanceCurrentItemsData["query"] = {},
  signal?: AbortSignal,
): Promise<OwnerAcceptanceCurrentItemsResponse> {
  const params = new URLSearchParams();
  if (query.limit) params.set("limit", String(query.limit));
  const qs = params.toString();
  return requestJson<OwnerAcceptanceCurrentItemsResponse>(
    `/v1/owner-acceptance/current-items${qs ? `?${qs}` : ""}`,
    "GET",
    undefined,
    signal,
  );
}

export type { OwnerAcceptanceDecision, OwnerAcceptanceProductDecision };
export type OwnerAcceptanceEventMutationResponse =
  OwnerAcceptanceEventResponse & {
    replayed: boolean;
  };

export function evaluateOwnerAcceptance(
  repository: string,
  pullRequestNumber: number,
  signal?: AbortSignal,
): Promise<EvaluateOwnerAcceptanceResponse> {
  const params = new URLSearchParams({
    repository,
    pull_request_number: String(pullRequestNumber),
  });
  return requestJson<EvaluateOwnerAcceptanceResponse>(
    `/v1/owner-acceptance/evaluation?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function writeOwnerAcceptanceEvent(
  payload: WriteOwnerAcceptanceEventData["body"],
  options: BrowserOperationOptions,
): Promise<OwnerAcceptanceEventMutationResponse> {
  const request: WriteOwnerAcceptanceEventData = {
    url: BROWSER_WRITE_ROUTES.ownerAcceptanceEvent,
    body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestGeneratedPost<OwnerAcceptanceEventResponse>(
    request,
    options.signal,
    options.onDispatch,
  ).then((response) => ({
    ...response,
    replayed: response.write_status === "replayed",
  }));
}

export type { PrivilegedOperationListResponse, PrivilegedOperationRecord };

export function readPrivilegedOperationPlans(
  signal?: AbortSignal,
): Promise<ListHumanPrivilegedOperationsResponse> {
  return requestJson<ListHumanPrivilegedOperationsResponse>(
    "/v1/privileged-operations/plans",
    "GET",
    undefined,
    signal,
  );
}

export function approvePrivilegedOperation(
  operationId: string,
  reason: string,
  signal?: AbortSignal,
): Promise<PrivilegedOperationHumanResponse> {
  const request: ApproveHumanPrivilegedOperationData = {
    url: BROWSER_WRITE_ROUTES.privilegedOperationApprove,
    path: { operation_id: operationId },
    body: { source_event_id: `ui:approve:${operationId}:${Date.now()}`, reason },
  };
  return requestJson<ApproveHumanPrivilegedOperationResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}/approve`,
    "POST",
    request.body,
    signal,
  );
}

export function revokePrivilegedOperation(
  operationId: string,
  reason: string,
  signal?: AbortSignal,
): Promise<PrivilegedOperationHumanResponse> {
  const request: RevokeHumanPrivilegedOperationData = {
    url: BROWSER_WRITE_ROUTES.privilegedOperationRevoke,
    path: { operation_id: operationId },
    body: { source_event_id: `ui:revoke:${operationId}:${Date.now()}`, reason },
  };
  return requestJson<RevokeHumanPrivilegedOperationResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}/revoke`,
    "POST",
    request.body,
    signal,
  );
}
