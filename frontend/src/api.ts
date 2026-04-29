import type {
  ApiErrorPayload,
  AuthSessionPayload,
  DriverListPayload,
  DriverViewPayload,
  LogoutPayload
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

async function requestJson<T>(path: string, method: "GET" | "POST" = "GET"): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json"
    }
  });
  const payload = (await response.json()) as T | ApiErrorPayload;
  if (!response.ok) {
    const errorPayload = payload as ApiErrorPayload;
    throw new LaunchplaneApiError(
      errorPayload.error?.message ?? `Launchplane API returned ${response.status}.`,
      response.status,
      errorPayload.trace_id
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

export function readDriverView(context: string, instance: string): Promise<DriverViewPayload> {
  const encodedContext = encodeURIComponent(context);
  if (!instance) {
    return requestJson<DriverViewPayload>(`/v1/contexts/${encodedContext}/driver-view`);
  }
  return requestJson<DriverViewPayload>(
    `/v1/contexts/${encodedContext}/instances/${encodeURIComponent(instance)}/driver-view`
  );
}
