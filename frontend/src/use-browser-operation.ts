import { useEffect, useRef, useState } from "react";

import {
  beginBrowserOperation,
  cancelBrowserOperation,
  completeBrowserOperation,
  createBrowserOperationState,
  failBrowserOperation,
  markBrowserOperationDispatched,
  persistBrowserOperationState,
  prepareBrowserOperation,
  recoverBrowserOperationState,
  resetBrowserOperation,
  reconcileBrowserOperation,
  type BrowserOperationIdentity,
  retryBrowserOperation,
  type BrowserOperationEnvelope,
  type BrowserOperationFailure,
  type BrowserOperationFailureCertainty,
  type BrowserOperationOptions,
  type BrowserOperationState,
} from "./browser-operation";

export interface BrowserOperationController<TPayload, TResponse> {
  cancel: () => void;
  reconcile: (identity: BrowserOperationIdentity, envelope: BrowserOperationEnvelope) => boolean;
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
  ) => BrowserOperationFailureCertainty;
  failureFor: (error: unknown) => BrowserOperationFailure;
  scope: string;
  readOnly?: boolean;
}

export function useBrowserOperationController<
  TPayload,
  TResponse extends BrowserOperationEnvelope,
>({
  execute,
  failureCertainty,
  failureFor,
  scope,
  readOnly = false,
}: BrowserOperationControllerOptions<TPayload, TResponse>): BrowserOperationController<
  TPayload,
  TResponse
> {
  const [state, setState] = useState<BrowserOperationState>(() =>
    recoverControllerState(scope, readOnly),
  );
  const stateRef = useRef(state);
  const scopeRef = useRef(scope);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const executeRef = useRef(execute);
  const failureCertaintyRef = useRef(failureCertainty);
  const failureForRef = useRef(failureFor);
  const readOnlyRef = useRef(readOnly);

  executeRef.current = execute;
  failureCertaintyRef.current = failureCertainty;
  failureForRef.current = failureFor;
  readOnlyRef.current = readOnly;

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
    updateState(recoverControllerState(scope, readOnly));
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
        current = readOnlyRef.current
          ? failBrowserOperation(current, failureForRef.current(error), "settled")
          : cancelBrowserOperation(current);
      } else {
        current = failBrowserOperation(
          current,
          failureForRef.current(error),
          readOnlyRef.current ? "settled" : failureCertaintyRef.current(error, dispatched),
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

  function reconcile(identity: BrowserOperationIdentity, envelope: BrowserOperationEnvelope) {
    try {
      updateState(reconcileBrowserOperation(stateRef.current, identity, envelope));
      return true;
    } catch { return false; }
  }

  return { cancel, reconcile, reset, run, state };
}

function recoverControllerState(scope: string, readOnly: boolean): BrowserOperationState {
  const recovered = recoverBrowserOperationState(scope);
  // The caller opts in only for side-effect-free requests; no mutation receipt can be lost.
  return readOnly && recovered.requiresIdempotencyContinuity ? createBrowserOperationState() : recovered;
}
