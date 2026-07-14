export type ResourceStatus = "idle" | "loading" | "ready" | "error";

export interface ResourceState<T> {
  status: ResourceStatus;
  data: T | null;
  error: string;
  traceId: string;
  statusCode: number;
}

export function emptyResource<T>(): ResourceState<T> {
  return {
    status: "idle",
    data: null,
    error: "",
    traceId: "",
    statusCode: 0,
  };
}
