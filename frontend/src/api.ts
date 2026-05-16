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
  LogoutPayload,
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
    throw new LaunchplaneApiError(
      errorPayload.error?.message ??
        `Launchplane API returned ${response.status}.`,
      response.status,
      errorPayload.trace_id,
    );
  }
  return payload as T;
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
  return requestJson<WorkGraphRankPayload>("/v1/work-graph/rank", "POST", {
    snapshot,
    limit,
  });
}

export function applyProductConfig(
  payload: ProductConfigApplyRequest,
  signal?: AbortSignal,
): Promise<ProductConfigApplyPayload> {
  return requestJson<ProductConfigApplyResponsePayload>(
    "/v1/product-config/apply",
    "POST",
    payload,
    signal,
  ).then((response) => response.result);
}

export function dryRunGenericWebProdPromotion(
  payload: GenericWebProdPromotionRequest,
): Promise<GenericWebProdPromotionPayload> {
  return requestJson<GenericWebProdPromotionPayload>(
    "/v1/drivers/generic-web/prod-promotion",
    "POST",
    payload,
  );
}

export function dispatchGenericWebPromotionWorkflow(
  payload: GenericWebPromotionWorkflowRequest,
): Promise<GenericWebPromotionWorkflowPayload> {
  return requestJson<GenericWebPromotionWorkflowPayload>(
    "/v1/drivers/generic-web/prod-promotion-workflow",
    "POST",
    payload,
  );
}
