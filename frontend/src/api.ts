import type {
  ApiErrorPayload,
  AuthSessionPayload,
  DriverListPayload,
  DriverViewPayload,
  EveryCodeSummaryPayload,
  EveryCodeWorkRequestListPayload,
  GenericWebProdPromotionPayload,
  GenericWebProdPromotionRequest,
  GenericWebPromotionWorkflowPayload,
  GenericWebPromotionWorkflowRequest,
  GitHubIssueInboxPayload,
  GitHubIssueInboxReconcileMode,
  GitHubIssueInboxReconcilePayload,
  LogoutPayload,
  MergeTrainControllerStatusPayload,
  MergeTrainPolicyTargetsPayload,
  ProductConfigApplyPayload,
  ProductConfigApplyRequest,
  ProductConfigApplyResponsePayload,
  ProductEnvironmentConfigStatusPayload,
  ProductListPayload,
  ProductProfileListPayload,
  PreviewReadinessPayload,
  RepoProductMappingPayload,
  WorkGraphRankPayload,
  WorkGraphSnapshot,
  WorkGraphSnapshotPayload,
} from "./types";
import type {
  ApplyGenericWebProdPromotionData,
  ApplyProductConfigData,
  DispatchGenericWebProdPromotionWorkflowData,
  RankWorkGraphSnapshotData,
  ReconcileWorkGraphIssueInboxData,
} from "./generated/openapi.ts";

export class LaunchplaneApiError extends Error {
  statusCode: number;
  traceId: string;

  constructor(message: string, statusCode: number, traceId = "") {
    super(message);
    this.name = "LaunchplaneApiError";
    this.statusCode = statusCode;
    this.traceId = traceId;
  }
}

async function requestJson<T>(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const headers: HeadersInit = {
    Accept: "application/json",
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
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
        ? errorPayload.error.message ?? `Launchplane API returned ${response.status}.`
        : `Launchplane API returned ${response.status}.`;
    const traceId =
      "trace_id" in errorPayload && typeof errorPayload.trace_id === "string"
        ? errorPayload.trace_id
        : "";
    throw new LaunchplaneApiError(
      errorMessage,
      response.status,
      traceId,
    );
  }
  return payload as T;
}

function requestGeneratedPost<
  T,
  TRequest extends { body: unknown; url: string } = { body: unknown; url: string },
>(
  request: TRequest,
  signal?: AbortSignal,
): Promise<T> {
  return requestJson<T>(request.url, "POST", request.body, signal);
}

export function readAuthSession(): Promise<AuthSessionPayload> {
  return requestJson<AuthSessionPayload>("/v1/auth/session");
}

export function logout(): Promise<LogoutPayload> {
  return requestJson<LogoutPayload>("/auth/logout", "POST");
}

export function listDrivers(): Promise<DriverListPayload> {
  return requestJson<DriverListPayload>("/v1/drivers");
}

export function readDriverView(
  context: string,
  instance: string,
): Promise<DriverViewPayload> {
  const encodedContext = encodeURIComponent(context);
  if (!instance) {
    return requestJson<DriverViewPayload>(
      `/v1/contexts/${encodedContext}/driver-view`,
    );
  }
  return requestJson<DriverViewPayload>(
    `/v1/contexts/${encodedContext}/instances/${encodeURIComponent(instance)}/driver-view`,
  );
}

export function listProductProfiles(
  driverId = "",
): Promise<ProductProfileListPayload> {
  const query = driverId ? `?driver_id=${encodeURIComponent(driverId)}` : "";
  return requestJson<ProductProfileListPayload>(`/v1/product-profiles${query}`);
}

export function listProducts(): Promise<ProductListPayload> {
  return requestJson<ProductListPayload>("/v1/products");
}

export function readProductEnvironmentConfigStatus(
  product: string,
  environment: string,
  signal?: AbortSignal,
): Promise<ProductEnvironmentConfigStatusPayload> {
  return requestJson<ProductEnvironmentConfigStatusPayload>(
    `/v1/products/${encodeURIComponent(product)}/environments/${encodeURIComponent(environment)}/config-status`,
    "GET",
    undefined,
    signal,
  );
}

export function listEveryCodeWorkRequests(
  limit = 8,
): Promise<EveryCodeWorkRequestListPayload> {
  return requestJson<EveryCodeWorkRequestListPayload>(
    `/v1/every-code/work-requests?limit=${encodeURIComponent(String(limit))}`,
  );
}

export function readEveryCodeSummary(
  limit = 12,
): Promise<EveryCodeSummaryPayload> {
  return requestJson<EveryCodeSummaryPayload>(
    `/v1/every-code/summary?limit=${encodeURIComponent(String(limit))}`,
  );
}

export function readPreviewReadiness(
  limit = 12,
): Promise<PreviewReadinessPayload> {
  return requestJson<PreviewReadinessPayload>(
    `/v1/previews/readiness?limit=${encodeURIComponent(String(limit))}`,
  );
}

export function readWorkGraphSnapshot(): Promise<WorkGraphSnapshotPayload> {
  return requestJson<WorkGraphSnapshotPayload>("/v1/work-graph/snapshot");
}

export function readRepoProductMapping(): Promise<RepoProductMappingPayload> {
  return requestJson<RepoProductMappingPayload>("/v1/repo-product-mapping");
}

export function rankWorkGraphSnapshot(
  snapshot: WorkGraphSnapshot,
  limit = 12,
): Promise<WorkGraphRankPayload> {
  const request: RankWorkGraphSnapshotData = {
    url: "/v1/work-graph/rank",
    body: {
      snapshot,
      limit,
    },
  };
  return requestGeneratedPost<WorkGraphRankPayload>(request);
}

export function readGitHubIssueInbox(
  signal?: AbortSignal,
): Promise<GitHubIssueInboxPayload> {
  return requestJson<GitHubIssueInboxPayload>(
    "/v1/work-graph/github/issues",
    "GET",
    undefined,
    signal,
  );
}

export function reconcileGitHubIssueInbox(
  mode: GitHubIssueInboxReconcileMode,
  signal?: AbortSignal,
): Promise<GitHubIssueInboxReconcilePayload> {
  const request: ReconcileWorkGraphIssueInboxData = {
    url: "/v1/work-graph/github/issues/reconcile",
    body: { mode },
  };
  return requestGeneratedPost<GitHubIssueInboxReconcilePayload>(request, signal);
}

export function readMergeTrainControllerStatus(
  repository: string,
  baseBranch: string,
  signal?: AbortSignal,
): Promise<MergeTrainControllerStatusPayload> {
  const params = new URLSearchParams({
    repository,
    base_branch: baseBranch,
  });
  return requestJson<MergeTrainControllerStatusPayload>(
    `/v1/work-graph/merge-train/controller/status?${params.toString()}`,
    "GET",
    undefined,
    signal,
  );
}

export function readMergeTrainPolicyTargets(
  signal?: AbortSignal,
): Promise<MergeTrainPolicyTargetsPayload> {
  return requestJson<MergeTrainPolicyTargetsPayload>(
    "/v1/work-graph/merge-train/policy-targets",
    "GET",
    undefined,
    signal,
  );
}

export function applyProductConfig(
  payload: ProductConfigApplyRequest,
  signal?: AbortSignal,
): Promise<ProductConfigApplyPayload> {
  const request: ApplyProductConfigData = {
    url: "/v1/product-config/apply",
    body: payload,
  };
  return requestGeneratedPost<ProductConfigApplyResponsePayload>(request, signal).then(
    (response) => response.result,
  );
}

export function dryRunGenericWebProdPromotion(
  payload: GenericWebProdPromotionRequest,
): Promise<GenericWebProdPromotionPayload> {
  const request: ApplyGenericWebProdPromotionData = {
    url: "/v1/drivers/generic-web/prod-promotion",
    body: payload,
  };
  return requestGeneratedPost<GenericWebProdPromotionPayload>(request);
}

export function dispatchGenericWebPromotionWorkflow(
  payload: GenericWebPromotionWorkflowRequest,
): Promise<GenericWebPromotionWorkflowPayload> {
  const request: DispatchGenericWebProdPromotionWorkflowData = {
    url: "/v1/drivers/generic-web/prod-promotion-workflow",
    body: payload,
  };
  return requestGeneratedPost<GenericWebPromotionWorkflowPayload>(request);
}
