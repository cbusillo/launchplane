import type {
  ApiErrorPayload,
  AuthSessionPayload,
  DriverListPayload,
  DriverViewPayload,
  GenericWebProdPromotionPayload,
  GenericWebProdPromotionRequest,
  LogoutPayload,
  ProductConfigApplyPayload,
  ProductConfigApplyRequest,
  ProductProfileListPayload,
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

export function applyProductConfig(
  payload: ProductConfigApplyRequest,
): Promise<ProductConfigApplyPayload> {
  return requestJson<ProductConfigApplyPayload>(
    "/v1/product-config/apply",
    "POST",
    payload,
  );
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
