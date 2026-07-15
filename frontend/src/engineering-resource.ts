import { useCallback, useEffect, useRef, useState } from "react";

export type EngineeringResourcePhase =
  | "idle"
  | "loading"
  | "ready"
  | "denied"
  | "error"
  | "cancelled";
export type EngineeringLoadReason = "initial" | "refresh";

export interface EngineeringResourceState<T> {
  phase: EngineeringResourcePhase;
  data: T | null;
  error: string;
  traceId: string;
  statusCode: number;
  refreshing: boolean;
  stale: boolean;
  cancelled: boolean;
  lastSuccessfulAt: string;
}

export interface EngineeringFailure {
  message: string;
  traceId: string;
  statusCode: number;
  denied: boolean;
}

export interface EngineeringResourceController<T> {
  state: EngineeringResourceState<T>;
  refresh: () => void;
  cancel: () => void;
}

export function emptyEngineeringResource<T>(): EngineeringResourceState<T> {
  return {
    phase: "idle",
    data: null,
    error: "",
    traceId: "",
    statusCode: 0,
    refreshing: false,
    stale: false,
    cancelled: false,
    lastSuccessfulAt: "",
  };
}

export function engineeringFailure(error: unknown): EngineeringFailure {
  const candidate =
    error && typeof error === "object"
      ? (error as {
          message?: unknown;
          traceId?: unknown;
          statusCode?: unknown;
        })
      : null;
  const statusCode =
    typeof candidate?.statusCode === "number" ? candidate.statusCode : 0;
  return {
    message:
      typeof candidate?.message === "string" && candidate.message.trim()
        ? candidate.message
        : "Launchplane could not load this Engineering Ops evidence.",
    traceId:
      typeof candidate?.traceId === "string" ? candidate.traceId.trim() : "",
    statusCode,
    denied: statusCode === 401 || statusCode === 403,
  };
}

export function cancelledEngineeringResource<T>(
  current: EngineeringResourceState<T>,
): EngineeringResourceState<T> {
  return current.data
    ? {
        ...current,
        phase: "ready",
        refreshing: false,
        stale: true,
        cancelled: true,
      }
    : {
        ...emptyEngineeringResource<T>(),
        phase: "cancelled",
        cancelled: true,
      };
}

export function engineeringResourceForScope<T>(
  current: EngineeringResourceState<T>,
  currentScopeKey: string,
  requestedScopeKey: string,
  enabled: boolean,
): EngineeringResourceState<T> {
  return enabled && currentScopeKey === requestedScopeKey
    ? current
    : emptyEngineeringResource<T>();
}

export function useEngineeringResource<T>(
  loader: (signal: AbortSignal, reason: EngineeringLoadReason) => Promise<T>,
  scopeKey: string,
  enabled = true,
) {
  const [state, setState] = useState<EngineeringResourceState<T>>(
    emptyEngineeringResource,
  );
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);
  const scopeKeyRef = useRef(scopeKey);

  const execute = useCallback(
    (preserveData: boolean) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      controllerRef.current = controller;
      setState((current) => {
        const data = preserveData ? current.data : null;
        return {
          ...emptyEngineeringResource<T>(),
          phase: data ? "ready" : "loading",
          data,
          refreshing: true,
          lastSuccessfulAt: data ? current.lastSuccessfulAt : "",
        };
      });

      void loader(controller.signal, preserveData ? "refresh" : "initial")
        .then((data) => {
          if (
            controller.signal.aborted ||
            requestIdRef.current !== requestId
          ) {
            return;
          }
          setState({
            phase: "ready",
            data,
            error: "",
            traceId: "",
            statusCode: 0,
            refreshing: false,
            stale: false,
            cancelled: false,
            lastSuccessfulAt: new Date().toISOString(),
          });
        })
        .catch((error: unknown) => {
          if (requestIdRef.current !== requestId) {
            return;
          }
          if (controller.signal.aborted || isAbortError(error)) {
            setState(cancelledEngineeringResource);
            return;
          }
          const failure = engineeringFailure(error);
          setState((current) =>
            current.data
              ? {
                  ...current,
                  phase: "ready",
                  error: failure.message,
                  traceId: failure.traceId,
                  statusCode: failure.statusCode,
                  refreshing: false,
                  stale: true,
                  cancelled: false,
                }
              : {
                  ...emptyEngineeringResource<T>(),
                  phase: failure.denied ? "denied" : "error",
                  error: failure.message,
                  traceId: failure.traceId,
                  statusCode: failure.statusCode,
                },
          );
        })
        .finally(() => {
          if (requestIdRef.current === requestId) {
            controllerRef.current = null;
          }
        });
    },
    [loader],
  );

  useEffect(() => {
    scopeKeyRef.current = scopeKey;
    if (!enabled) {
      requestIdRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
      setState(emptyEngineeringResource());
      return;
    }
    execute(false);
    return () => {
      requestIdRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [enabled, execute, scopeKey]);

  const refresh = useCallback(() => execute(true), [execute]);
  const cancel = useCallback(() => {
    const controller = controllerRef.current;
    if (!controller) {
      return;
    }
    requestIdRef.current += 1;
    controllerRef.current = null;
    controller.abort();
    setState(cancelledEngineeringResource);
  }, []);

  return {
    state: engineeringResourceForScope(
      state,
      scopeKeyRef.current,
      scopeKey,
      enabled,
    ),
    refresh,
    cancel,
  } satisfies EngineeringResourceController<T>;
}

function isAbortError(error: unknown): boolean {
  return (
    error instanceof DOMException
      ? error.name === "AbortError"
      : Boolean(
          error &&
            typeof error === "object" &&
            "name" in error &&
            (error as { name?: unknown }).name === "AbortError",
        )
  );
}
