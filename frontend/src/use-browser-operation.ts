import { useEffect, useRef, useState } from "react";

import {
  beginBrowserOperation,
  cancelBrowserOperation,
  completeBrowserOperation,
  failBrowserOperation,
  markBrowserOperationDispatched,
  persistBrowserOperationState,
  prepareBrowserOperation,
  recoverBrowserOperationState,
  resetBrowserOperation,
  retryBrowserOperation,
  type BrowserOperationEnvelope,
  type BrowserOperationFailure,
  type BrowserOperationOptions,
  type BrowserOperationState,
} from "./browser-operation";

export interface BrowserOperationController<TPayload, TResponse> {
  cancel: () => void;
  reset: () => boolean;
  run: (payload: TPayload) => Promise<TResponse | null>;
  state: BrowserOperationState;
}

interface BrowserOperationControllerOptions<TPayload, TResponse> {
  execute: (
    payload: TPayload,
    options: BrowserOperationOptions,
  ) => Promise<TResponse>;
  failureCertainty: (
    error: unknown,
    dispatched: boolean,
  ) => "definitive" | "uncertain";
  failureFor: (error: unknown) => BrowserOperationFailure;
  scope: string;
}

export function useBrowserOperationController<
  TPayload,
  TResponse extends BrowserOperationEnvelope,
>({
  execute,
  failureCertainty,
  failureFor,
  scope,
}: BrowserOperationControllerOptions<TPayload, TResponse>): BrowserOperationController<
  TPayload,
  TResponse
> {
  const [state, setState] = useState<BrowserOperationState>(() =>
    recoverBrowserOperationState(scope),
  );
  const stateRef = useRef(state);
  const scopeRef = useRef(scope);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const executeRef = useRef(execute);
  const failureCertaintyRef = useRef(failureCertainty);
  const failureForRef = useRef(failureFor);

  executeRef.current = execute;
  failureCertaintyRef.current = failureCertainty;
  failureForRef.current = failureFor;

  function updateState(next: BrowserOperationState) {
    stateRef.current = next;
    persistBrowserOperationState(scope, next);
    if (mountedRef.current) {
      setState(next);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (scopeRef.current === scope) {
      return;
    }
    abortRef.current?.abort();
    scopeRef.current = scope;
    updateState(recoverBrowserOperationState(scope));
  }, [scope]);

  async function run(payload: TPayload): Promise<TResponse | null> {
    let current = stateRef.current;
    if (current.phase === "succeeded") {
      current = resetBrowserOperation(current);
    } else if (["failed", "uncertain", "cancelled"].includes(current.phase)) {
      current = retryBrowserOperation(current);
    }
    try {
      current = await prepareBrowserOperation(scope, payload, current);
      current = beginBrowserOperation(current);
      updateState(current);
    } catch (error) {
      updateState({
        ...current,
        failure: failureForRef.current(error),
        phase: current.requiresIdempotencyContinuity ? "uncertain" : "failed",
      });
      return null;
    }

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    let dispatched = false;
    try {
      const response = await executeRef.current(payload, {
        idempotencyKey: current.identity?.idempotencyKey ?? "",
        signal: controller.signal,
        onDispatch: () => {
          dispatched = true;
          current = markBrowserOperationDispatched(current);
          updateState(current);
        },
      });
      if (!dispatched) {
        current = markBrowserOperationDispatched(current);
      }
      current = completeBrowserOperation(current, response);
      updateState(current);
      return response;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        current = cancelBrowserOperation(current);
      } else {
        current = failBrowserOperation(
          current,
          failureForRef.current(error),
          failureCertaintyRef.current(error, dispatched),
        );
      }
      updateState(current);
      return null;
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  function reset() {
    try {
      updateState(resetBrowserOperation(stateRef.current));
      return true;
    } catch {
      return false;
    }
  }

  return { cancel, reset, run, state };
}
