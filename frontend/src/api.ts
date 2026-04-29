import type { ApiErrorPayload, DriverListPayload, DriverViewPayload } from "./types";

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

async function readJson<T>(path: string, bearerToken: string): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${bearerToken}`
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

export function listDrivers(bearerToken: string): Promise<DriverListPayload> {
  return readJson<DriverListPayload>("/v1/drivers", bearerToken);
}

export function readDriverView(
  bearerToken: string,
  context: string,
  instance: string
): Promise<DriverViewPayload> {
  const encodedContext = encodeURIComponent(context);
  if (!instance) {
    return readJson<DriverViewPayload>(`/v1/contexts/${encodedContext}/driver-view`, bearerToken);
  }
  return readJson<DriverViewPayload>(
    `/v1/contexts/${encodedContext}/instances/${encodeURIComponent(instance)}/driver-view`,
    bearerToken
  );
}
