import type { BackupCredentialBindingRequest, AcceptedEvidenceResponse, QualifyProductionBackupPreflightData } from "./generated/openapi.ts";
import type { DiscussionTopicsResponse } from "./generated/openapi.ts";

export function readDiscussionTopics(product: string, signal?: AbortSignal): Promise<DiscussionTopicsResponse> {
  return requestJson<DiscussionTopicsResponse>(`/v1/products/${encodeURIComponent(product)}/discussion-topics`, "GET", undefined, signal);
}
import type {
  ApiErrorPayload,
  AuthSessionPayload,
  LogoutPayload,
  ProductListPayload,
} from "./types";
import type { InspectTestingRebuildData, InspectTestingRebuildResponse, ReadTestingRebuildResponse, SubmitTestingRebuildData, SubmitTestingRebuildResponse, LookupTestingRebuildData, LookupTestingRebuildResponse, ResumeTestingRebuildData, ResumeTestingRebuildResponse } from "./generated/openapi.ts";
import type {
  ApplyProductionBackupAuthorityData,
  ApplyProductionBackupAuthorityResponse,
  ReadProductionBackupAuthorityData,
  ReadProductionBackupAuthorityResponse,
  ApplyProductHealthMonitoringData,
  ApplyProductHealthMonitoringResponse,
  ApplyProductEnvironmentConfigData,
  ApplyProductGenericWebTransitionData,
  ApplyProductGenericWebTransitionResponse,
  ApplyProductDisplayNameData,
  ApplyProductDisplayNameResponse,
  ApplyProductWebsiteCleanupData,
  ApplyProductWebsiteCleanupResponse,
  ApplyProductEnvironmentConfigResponse,
  ApplyProductImageRepositoryData,
  ApplyProductImageRepositoryResponse,
  ApplyProductProductionUseData,
  ApplyProductProductionUseResponse,
  ApplyProductOwnerData,
  ApplyProductOwnerResponse,
  ApproveHumanPrivilegedOperationData,
  ApproveHumanPrivilegedOperationResponse,
  CancelHumanPrivilegedOperationData,
  CancelHumanPrivilegedOperationResponse,
  DispatchProductPromotionWorkflowData,
  DispatchProductPromotionWorkflowResponse,
  DryRunProductPromotionData,
  DryRunProductPromotionResponse,
  EnqueueOdooProdPromotionData,
  EnqueueOdooProdPromotionResponse,
  EnqueueOdooProdRollbackData,
  EnqueueOdooProdRollbackResponse,
  EnqueueProductionBackupGateData,
  EnqueueProductionBackupGateResponse,
  EveryCodeSummaryResponse,
  GovernanceProjectionResponse,
  ListHumanPrivilegedOperationsData,
  ListHumanPrivilegedOperationsResponse,
  ListProductProfilesData,
  ListProductProfilesResponse,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
  PrepareAuthorizationCandidateData,
  PrepareAuthorizationCandidateResponse,
  ProductActivityResponse,
  ProductEnvironmentConfigStatusResponse,
  ProductEnvironmentIncidentResponse,
  ProductEnvironmentIncidentsResponse,
  ProductOperationalReadinessResponse,
  ProductEnvironmentResponse,
  ProductOverviewResponse,
  PreviewHistoryResponse,
  ProductReconcileRequestsResponse,
  ProductPromotionStatusResponse,
  ProductPromotionWorkflowDeliveryStatusResponse,
  PrivilegedOperationHumanResponse,
  PrivilegedOperationListResponse,
  PrivilegedOperationHumanRecord as PrivilegedOperationRecord,
  PrivilegedOperationSemanticReview,
  PrivilegedOperationSemanticReviewResponse,
  ReadHumanPrivilegedOperationData,
  ReadHumanPrivilegedOperationResponse,
  ReadHumanPrivilegedOperationReviewData,
  ReadHumanPrivilegedOperationReviewResponse,
  RankWorkGraphSnapshotData,
  RankWorkGraphSnapshotResponse,
  ReadProductOperationalReadinessData,
  ReadProductProfileData,
  ReadProductProfileResponse,
  ReadProductProfileMutationReceiptData,
  ReadProductProfileMutationReceiptResponse,
  ReadTenantAdmissionEvaluationData,
  RevokeHumanPrivilegedOperationData,
  RevokeHumanPrivilegedOperationResponse,
  TenantAdmissionEvaluationReadResponse,
  WorkGraphIssueInboxResponse,
  WorkGraphSnapshot,
  WorkGraphSnapshotResponse,
  ProductReviewResponse,
  OwnerSecretInputResponse,
  SubmitOwnerSecretInputData,
  WriteProductReviewDecisionData,
  RetryProductReviewFeedbackData,
  ReleaseReviewResponse,
  ReleaseReviewDecisionEnvelope,
  UrgentReleaseReviewEnvelope,
  ReadProductionBackupGateOperationData,
  ReadProductionBackupGateOperationResponse,
  ReadOdooProdPromotionOperationData,
  ReadOdooProdPromotionOperationResponse,
  ReadOdooProdRollbackOperationData,
  ReadOdooProdRollbackOperationResponse,
} from "./generated/openapi.ts";

import type { BrowserOperationOptions } from "./browser-operation";
import type { RestartLaneServiceData, LaneServiceRestartResponse } from "./generated/openapi.ts";
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
const sessionExpiryListeners = new Set<() => void>();

export function subscribeToSessionExpiry(listener: () => void): () => void {
  sessionExpiryListeners.add(listener);
  return () => { sessionExpiryListeners.delete(listener); };
}

export function deliverTestingLocalConsumer(
  payload: import("./generated/openapi.ts").ConsumerDeliveryEnvelope,
): Promise<import("./generated/openapi.ts").ConsumerDeliveryResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.localConsumerDelivery, "POST", payload);
}

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
  verificationRetries = 0,
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
    if (method === "GET" && response.status === 503 &&
        errorCode === "identity_verification_pending" && verificationRetries < 40 && !signal?.aborted) {
      await new Promise<void>(resolve => setTimeout(resolve, 1000));
      signal?.throwIfAborted();
      return performJsonRequest<T>(path, method, body, signal, csrfToken, idempotencyKey, verificationRetries + 1);
    }
    if (method === "GET" && response.status === 401 &&
        errorCode === "authentication_required" && !signal?.aborted) {
      sessionExpiryListeners.forEach(listener => listener());
    }
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

export async function logout(): Promise<LogoutPayload> {
  try {
    return await requestJson<LogoutPayload>("/auth/logout", "POST");
  } catch (error) {
    // An authentication-required preflight means the server session already
    // ended. Complete local sign-out without sending or replaying a write.
    if (error instanceof LaunchplaneApiError && error.statusCode === 401 &&
        error.code === "authentication_required") {
      return { status: "ok", trace_id: error.traceId };
    }
    throw error;
  }
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

export function readPreviewHistory(previewId: string, signal?: AbortSignal): Promise<PreviewHistoryResponse> {
  return requestJson(`/v1/previews/${encodeURIComponent(previewId)}/history`, "GET", undefined, signal);
}

export function readProductReconcileRequests(product: string, signal?: AbortSignal): Promise<ProductReconcileRequestsResponse> {
  return requestJson(`/v1/product-profiles/${encodeURIComponent(product)}/reconcile-requests`, "GET", undefined, signal);
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

export function restartLaneService(
  payload: RestartLaneServiceData["body"],
  idempotencyKey = "",
): Promise<LaneServiceRestartResponse> {
  const request: RestartLaneServiceData = {
    url: BROWSER_WRITE_ROUTES.laneServiceRestart,
    body: payload,
    headers: { "Idempotency-Key": idempotencyKey },
  };
  return requestJson(request.url, "POST", request.body, undefined, idempotencyKey);
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

export function applyProductHealthMonitoring(
  payload: ApplyProductHealthMonitoringData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductHealthMonitoringResponse> {
  return requestJson<ApplyProductHealthMonitoringResponse>(
    BROWSER_WRITE_ROUTES.productHealthMonitoringApply,
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
  );
}

export function repairDomainAuthority(
  payload: import("./generated/openapi.ts").SetupDokployTargetData["body"],
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").SetupDokployTargetResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.domainAuthorityRepair, "POST", payload,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function repairRouteBinding(
  payload: import("./generated/openapi.ts").ReconcileRouteBindingData["body"],
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").ReconcileRouteBindingResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.routeBindingRepair, "POST", payload,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function applyProductionBackupAuthority(
  payload: ApplyProductionBackupAuthorityData["body"], options: BrowserOperationOptions,
): Promise<ApplyProductionBackupAuthorityResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.productionBackupAuthorityApply, "POST", payload,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function readProductionBackupAuthority(
  query: ReadProductionBackupAuthorityData["query"], signal?: AbortSignal,
): Promise<ReadProductionBackupAuthorityResponse> {
  const request: ReadProductionBackupAuthorityData = { url: "/v1/production-backup-authority", query };
  const parameters = new URLSearchParams({ product: query.product, context: query.context,
    instance: query.instance, promotion_action: query.promotion_action });
  return requestJson(`${request.url}?${parameters}`, "GET", undefined, signal);
}

export function listProductProfiles(
  signal?: AbortSignal,
): Promise<ListProductProfilesResponse> {
  const request: ListProductProfilesData = { url: "/v1/product-profiles" };
  return requestJson<ListProductProfilesResponse>(
    request.url,
    "GET",
    undefined,
    signal,
  );
}

export function readProductProfile(
  product: string,
  signal?: AbortSignal,
): Promise<ReadProductProfileResponse> {
  const request: ReadProductProfileData = {
    url: "/v1/product-profiles/{product}",
    path: { product },
  };
  return requestJson<ReadProductProfileResponse>(
    `/v1/product-profiles/${encodeURIComponent(request.path.product)}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductProfileMutationReceipt(
  product: string,
  field: ReadProductProfileMutationReceiptData["path"]["field"],
  operationKey: string,
  signal?: AbortSignal,
): Promise<ReadProductProfileMutationReceiptResponse> {
  return requestJson<ReadProductProfileMutationReceiptResponse>(
    `/v1/product-profiles/${encodeURIComponent(product)}/mutation-receipts/${field}?operation_key=${encodeURIComponent(operationKey)}`,
    "GET", undefined, signal,
  );
}

export function applyProductOwner(
  product: string,
  payload: ApplyProductOwnerData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductOwnerResponse> {
  const request: ApplyProductOwnerData = {
    url: BROWSER_WRITE_ROUTES.productOwnerApply,
    path: { product },
    body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson<ApplyProductOwnerResponse>(
    `/v1/product-profiles/${encodeURIComponent(request.path.product)}/owner`,
    "POST",
    request.body,
    options.signal,
    generatedIdempotencyKey(request.headers),
    options.onDispatch,
  );
}

export function applyProductGenericWebTransition(
  product: string,
  payload: ApplyProductGenericWebTransitionData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductGenericWebTransitionResponse> {
  return requestJson<ApplyProductGenericWebTransitionResponse>(
    BROWSER_WRITE_ROUTES.productGenericWebTransition.replace("{product}", encodeURIComponent(product)),
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
  );
}

export function applyProductDisplayName(
  product: string,
  payload: ApplyProductDisplayNameData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductDisplayNameResponse> {
  return requestJson<ApplyProductDisplayNameResponse>(
    BROWSER_WRITE_ROUTES.productDisplayName.replace("{product}", encodeURIComponent(product)),
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
  );
}

export function applyProductWebsiteCleanup(
  product: string,
  payload: ApplyProductWebsiteCleanupData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductWebsiteCleanupResponse> {
  return requestJson<ApplyProductWebsiteCleanupResponse>(
    BROWSER_WRITE_ROUTES.productWebsiteCleanup.replace("{product}", encodeURIComponent(product)),
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
  );
}

export function applyProductImageRepository(
  product: string,
  payload: ApplyProductImageRepositoryData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductImageRepositoryResponse> {
  return requestJson<ApplyProductImageRepositoryResponse>(
    BROWSER_WRITE_ROUTES.productImageRepositoryApply.replace("{product}", encodeURIComponent(product)),
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
  );
}

export function applyProductProductionUse(
  product: string,
  payload: ApplyProductProductionUseData["body"],
  options: BrowserOperationOptions,
): Promise<ApplyProductProductionUseResponse> {
  return requestJson<ApplyProductProductionUseResponse>(
    BROWSER_WRITE_ROUTES.productProductionUseApply.replace("{product}", encodeURIComponent(product)),
    "POST", payload, options.signal, options.idempotencyKey, options.onDispatch,
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

export function enqueueProductionBackupGate(
  payload: EnqueueProductionBackupGateData["body"],
  options: BrowserOperationOptions,
): Promise<EnqueueProductionBackupGateResponse> {
  return requestGeneratedPost<EnqueueProductionBackupGateResponse>(
    {
      url: BROWSER_WRITE_ROUTES.productionBackupGateEnqueue,
      body: payload,
      headers: { "Idempotency-Key": options.idempotencyKey },
    },
    options.signal,
    options.onDispatch,
  );
}

export function readProductionBackupGateOperation(
  operationId: string,
  scope: ReadProductionBackupGateOperationData["query"],
  signal?: AbortSignal,
): Promise<ReadProductionBackupGateOperationResponse> {
  return requestJson<ReadProductionBackupGateOperationResponse>(
    `/v1/production-backup-gates/operations/${encodeURIComponent(operationId)}?${new URLSearchParams(scope)}`,
    "GET",
    undefined,
    signal,
  );
}

export function enqueueOdooProdPromotion(
  payload: EnqueueOdooProdPromotionData["body"],
  options: BrowserOperationOptions,
): Promise<EnqueueOdooProdPromotionResponse> {
  return requestGeneratedPost<EnqueueOdooProdPromotionResponse>(
    {
      url: BROWSER_WRITE_ROUTES.odooProdPromotionEnqueue,
      body: payload,
      headers: { "Idempotency-Key": options.idempotencyKey },
    },
    options.signal,
    options.onDispatch,
  );
}

export function inspectTestingRebuild(
  product: string,
  source: NonNullable<InspectTestingRebuildData["query"]>["data_source_mode"],
  signal?: AbortSignal,
): Promise<InspectTestingRebuildResponse> {
  return requestJson(`/v1/products/${encodeURIComponent(product)}/testing-rebuild/inputs?data_source_mode=${encodeURIComponent(source ?? "empty")}`, "GET", undefined, signal);
}

export function submitTestingRebuild(
  product: string,
  payload: SubmitTestingRebuildData["body"],
  options: BrowserOperationOptions,
): Promise<SubmitTestingRebuildResponse> {
  const request: SubmitTestingRebuildData = {
    url: BROWSER_WRITE_ROUTES.testingRebuild, path: { product }, body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson(request.url.replace("{product}", encodeURIComponent(product)), "POST", request.body,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function readTestingRebuild(product: string, operationId: string, signal?: AbortSignal): Promise<ReadTestingRebuildResponse> {
  return requestJson(`/v1/products/${encodeURIComponent(product)}/testing-rebuild/operations/${encodeURIComponent(operationId)}`, "GET", undefined, signal);
}

export function lookupTestingRebuild(product: string, payload: LookupTestingRebuildData["body"], options: BrowserOperationOptions): Promise<LookupTestingRebuildResponse> {
  const request: LookupTestingRebuildData = {
    url: BROWSER_WRITE_ROUTES.testingRebuildLookup, path: { product }, body: payload,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestJson(request.url.replace("{product}", encodeURIComponent(product)), "POST", request.body,
    options.signal, options.idempotencyKey);
}

export function resumeTestingRebuild(product: string, operationId: string, payload: ResumeTestingRebuildData["body"], options: BrowserOperationOptions): Promise<ResumeTestingRebuildResponse> {
  const path = BROWSER_WRITE_ROUTES.testingRebuildResume.replace("{product}", encodeURIComponent(product)).replace("{operation_id}", encodeURIComponent(operationId));
  return requestJson(path, "POST", payload, options.signal, options.idempotencyKey, options.onDispatch);
}

export function readOdooProdPromotionOperation(
  operationId: string,
  scope: ReadOdooProdPromotionOperationData["query"],
  signal?: AbortSignal,
): Promise<ReadOdooProdPromotionOperationResponse> {
  return requestJson<ReadOdooProdPromotionOperationResponse>(
    `/v1/odoo-prod-promotions/operations/${encodeURIComponent(operationId)}?${new URLSearchParams(scope)}`,
    "GET",
    undefined,
    signal,
  );
}

export function enqueueOdooProdRollback(
  payload: EnqueueOdooProdRollbackData["body"],
  options: BrowserOperationOptions,
): Promise<EnqueueOdooProdRollbackResponse> {
  return requestGeneratedPost<EnqueueOdooProdRollbackResponse>(
    {
      url: BROWSER_WRITE_ROUTES.odooProdRollbackEnqueue,
      body: payload,
      headers: { "Idempotency-Key": options.idempotencyKey },
    },
    options.signal,
    options.onDispatch,
  );
}

export function readOdooProdRollbackOperation(
  operationId: string,
  scope: ReadOdooProdRollbackOperationData["query"],
  signal?: AbortSignal,
): Promise<ReadOdooProdRollbackOperationResponse> {
  return requestJson<ReadOdooProdRollbackOperationResponse>(
    `/v1/odoo-prod-rollbacks/operations/${encodeURIComponent(operationId)}?${new URLSearchParams(scope)}`,
    "GET",
    undefined,
    signal,
  );
}

export function readProductReview(
  repository: string,
  pullRequest: number,
  signal?: AbortSignal,
  decisionId?: string,
): Promise<ProductReviewResponse> {
  const params = new URLSearchParams({
    repository,
    pull_request: String(pullRequest),
  });
  if (decisionId) params.set("decision_id", decisionId);
  return requestJson<ProductReviewResponse>(
    `/v1/product-review?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function readOwnerSecretInputs(product: string, environment: string, signal?: AbortSignal): Promise<OwnerSecretInputResponse> {
  return requestJson<OwnerSecretInputResponse>(`/v1/owner-secret-inputs?${new URLSearchParams({ product, environment })}`, "GET", undefined, signal);
}

export function submitOwnerSecretInput(payload: SubmitOwnerSecretInputData["body"]): Promise<OwnerSecretInputResponse> {
  return requestJson<OwnerSecretInputResponse>(BROWSER_WRITE_ROUTES.ownerSecretSubmit, "POST", payload);
}

export function readReleaseReview(product: string, signal?: AbortSignal): Promise<ReleaseReviewResponse> {
  return requestJson<ReleaseReviewResponse>(`/v1/release-review?${new URLSearchParams({ product })}`, "GET", undefined, signal);
}

export function writeReleaseReviewDecision(payload: ReleaseReviewDecisionEnvelope): Promise<ReleaseReviewResponse> {
  return requestJson<ReleaseReviewResponse>(BROWSER_WRITE_ROUTES.releaseReviewDecision, "POST", payload);
}

export function openUrgentReleaseReview(payload: UrgentReleaseReviewEnvelope): Promise<ReleaseReviewResponse> {
  return requestJson<ReleaseReviewResponse>(BROWSER_WRITE_ROUTES.releaseReviewUrgentOpening, "POST", payload);
}

export function writeProductReviewDecision(
  payload: WriteProductReviewDecisionData["body"],
  signal?: AbortSignal,
): Promise<ProductReviewResponse> {
  const request: WriteProductReviewDecisionData = {
    url: BROWSER_WRITE_ROUTES.productReviewDecision,
    body: payload,
  };
  return requestJson<ProductReviewResponse>(
    request.url,
    "POST",
    request.body,
    signal,
  );
}

export function retryProductReviewFeedback(
  payload: RetryProductReviewFeedbackData["body"],
): Promise<ProductReviewResponse> {
  return requestJson<ProductReviewResponse>(
    BROWSER_WRITE_ROUTES.productReviewFeedbackRetry, "POST", payload,
  );
}


export type {
  PrivilegedOperationHumanResponse,
  PrivilegedOperationListResponse,
  PrivilegedOperationRecord,
  PrivilegedOperationSemanticReview,
  PrivilegedOperationSemanticReviewResponse,
};




export type PrivilegedOperationDescriptorId = NonNullable<
  ListHumanPrivilegedOperationsData["query"]
>["descriptor_id"];

export type AuthorizationCandidateId =
  PrepareAuthorizationCandidateData["body"]["candidate_id"];

export type AuthorizationCandidateIntent =
  PrepareAuthorizationCandidateData["body"]["intent"];

export function prepareAuthorizationCandidate(
  candidateId: AuthorizationCandidateId,
  intent: AuthorizationCandidateIntent,
  sourceEventId: string,
  signal?: AbortSignal,
  products?: readonly string[],
): Promise<PrepareAuthorizationCandidateResponse> {
  const body: PrepareAuthorizationCandidateData["body"] = {
    candidate_id: candidateId,
    intent,
    source_event_id: sourceEventId,
    ...(products === undefined ? {} : { products: [...products] }),
  };
  return requestJson<PrepareAuthorizationCandidateResponse>(
    BROWSER_WRITE_ROUTES.authorizationCandidatePrepare,
    "POST",
    body,
    signal,
  );
}

export function readPrivilegedOperationPlans(
  signal?: AbortSignal,
  descriptorId: PrivilegedOperationDescriptorId = "managed-secret-reencryption",
): Promise<ListHumanPrivilegedOperationsResponse> {
  const query =
    descriptorId === "managed-secret-reencryption"
      ? ""
      : `?descriptor_id=${encodeURIComponent(descriptorId)}`;
  return requestJson<ListHumanPrivilegedOperationsResponse>(
    `/v1/privileged-operations/plans${query}`,
    "GET",
    undefined,
    signal,
  );
}




export function readPrivilegedOperationReview(
  operationId: string,
  signal?: AbortSignal,
): Promise<PrivilegedOperationSemanticReviewResponse> {
  const request: ReadHumanPrivilegedOperationReviewData = {
    path: { operation_id: operationId },
    url: "/v1/privileged-operations/plans/{operation_id}/review",
  };
  return requestJson<ReadHumanPrivilegedOperationReviewResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}/review`,
    "GET",
    undefined,
    signal,
  );
}

export function readPrivilegedOperationRawDetail(
  operationId: string,
  signal?: AbortSignal,
): Promise<PrivilegedOperationHumanResponse> {
  const request: ReadHumanPrivilegedOperationData = {
    path: { operation_id: operationId },
    url: "/v1/privileged-operations/plans/{operation_id}",
  };
  return requestJson<ReadHumanPrivilegedOperationResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}`,
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
    body: {
      source_event_id: `ui:approve:${operationId}:${Date.now()}`,
      reason,
    },
  };
  return requestJson<ApproveHumanPrivilegedOperationResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}/approve`,
    "POST",
    request.body,
    signal,
  );
}

export function cancelPrivilegedOperation(
  operationId: string,
  reason: string,
  sourceEventId: string,
  signal?: AbortSignal,
): Promise<PrivilegedOperationHumanResponse> {
  const request: CancelHumanPrivilegedOperationData = {
    url: BROWSER_WRITE_ROUTES.privilegedOperationCancel,
    path: { operation_id: operationId },
    body: { source_event_id: sourceEventId, reason },
  };
  return requestJson<CancelHumanPrivilegedOperationResponse>(
    `/v1/privileged-operations/plans/${encodeURIComponent(request.path.operation_id)}/cancel`,
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







export function readServiceGitHubDelivery(signal?: AbortSignal): Promise<import("./generated/openapi.ts").ServiceGitHubDeliveryStatus> {
  return requestJson("/v1/service/github-delivery", "GET", undefined, signal);
}

export function configureServiceGitHubDelivery(
  body: import("./generated/openapi.ts").DeliveryGitHubAppConfigurationRequest,
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").DeliveryGitHubAppConfigurationResponse> {
  const request: import("./generated/openapi.ts").ConfigureLaunchplaneGithubDeliveryData = {
    url: BROWSER_WRITE_ROUTES.serviceGitHubDeliveryConfigure,
    body,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestGeneratedPost(request, options.signal, options.onDispatch);
}

export function retireServiceGitHubTokens(
  body: import("./generated/openapi.ts").ServiceTokenRetirementRequest,
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").ServiceTokenRetirementResponse> {
  const request: import("./generated/openapi.ts").RetireServiceGithubTokensData = {
    url: BROWSER_WRITE_ROUTES.serviceGitHubTokenRetire,
    body,
    headers: { "Idempotency-Key": options.idempotencyKey },
  };
  return requestGeneratedPost(request, options.signal, options.onDispatch);
}

export function checkServiceDeliveryApplyReceipt(
  originalRequest: import("./generated/openapi.ts").ServiceDeliveryApplyReceiptRequest["original_request"], idempotencyKey: string, signal: AbortSignal,
): Promise<import("./generated/openapi.ts").ServiceDeliveryApplyReceiptResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.serviceDeliveryReceiptRead, "POST", { original_request: originalRequest, idempotency_key: idempotencyKey }, signal);
}

export function applyProductExpectedConfig(
  payload: import("./generated/openapi.ts").ApplyProductExpectedConfigData["body"],
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").AcceptedEvidenceResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.productExpectedConfigApply, "POST", payload,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function applyProductStableLaneRepair(
  payload: import("./generated/openapi.ts").ApplyProductStableLaneRepairData["body"],
  options: BrowserOperationOptions,
): Promise<import("./generated/openapi.ts").AcceptedEvidenceResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.productStableLaneRepairApply, "POST", payload,
    options.signal, options.idempotencyKey, options.onDispatch);
}

export function executeProductRetirement(payload: import("./generated/openapi.ts").ExecuteProductRetirementData["body"], options: BrowserOperationOptions): Promise<import("./generated/openapi.ts").AcceptedEvidenceResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.productRetirement, "POST", payload, options.signal, options.idempotencyKey, options.onDispatch);
}

export function readBackupWorkerBindings(product: string, signal?: AbortSignal): Promise<AcceptedEvidenceResponse> {
  return requestJson(`/v1/products/${encodeURIComponent(product)}/environments/prod/backup-worker-bindings`, "GET", undefined, signal);
}
export function rebindBackupWorkerCredentials(product: string, body: BackupCredentialBindingRequest, options: BrowserOperationOptions): Promise<AcceptedEvidenceResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.backupWorkerBindings.replace("{product}", encodeURIComponent(product)), "POST", body, options.signal, options.idempotencyKey, options.onDispatch);
}
export function qualifyProductionBackupPreflight(query: QualifyProductionBackupPreflightData["query"]): Promise<AcceptedEvidenceResponse> {
  return requestJson(`/v1/production-backup-authority/preflight?${new URLSearchParams(query)}`, "GET");
}

export function readBackupPolicyProposals(scope: { product: string; context: string; environment: string }, signal?: AbortSignal): Promise<import("./generated/openapi.ts").BackupPolicyProposalListResponse> {
  const query = new URLSearchParams({ product: scope.product, context: scope.context, instance: scope.environment });
  return requestJson(`/v1/production-backup-authority/proposals?${query}`, "GET", undefined, signal);
}

export function decideBackupPolicyProposal(payload: { operationId: string; decision: "approve" | "decline" }, options: BrowserOperationOptions): Promise<import("./generated/openapi.ts").BackupPolicyDecisionResponse> {
  return requestJson(BROWSER_WRITE_ROUTES.backupPolicyDecision.replace("{operation_id}", encodeURIComponent(payload.operationId)), "POST", {
    decision: payload.decision, source_event_id: `ui:${payload.decision}:${payload.operationId}`,
  }, options.signal, options.idempotencyKey, options.onDispatch);
}
