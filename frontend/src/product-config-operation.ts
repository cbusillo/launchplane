import { LaunchplaneApiError } from "./api";
import type { BrowserOperationFailure, BrowserOperationState } from "./browser-operation";
import type { ProductEnvironmentManagedSecretInput } from "./generated/openapi.ts";

export interface SecretValueInput {
  value: string;
}

export interface ManagedSecretSelection {
  bindingKey: string;
  integration: string;
  identity: string;
}

export function productConfigManagedSecretIdentity(
  integration: string,
  bindingKey: string,
): string {
  return JSON.stringify([integration, bindingKey]);
}

export function consumeManagedSecretValues(
  selections: readonly ManagedSecretSelection[],
  inputs: ReadonlyMap<string, SecretValueInput>,
): ProductEnvironmentManagedSecretInput[] {
  try {
    return selections.map(({ bindingKey, integration, identity }) => {
      const value = inputs.get(identity)?.value ?? "";
      if (!value.trim()) {
        throw new Error(`Enter a value for ${bindingKey} (${integration}).`);
      }
      return { binding_key: bindingKey, integration, value };
    });
  } finally {
    clearManagedSecretInputs(inputs);
  }
}

export function clearManagedSecretInputs(
  inputs: ReadonlyMap<string, SecretValueInput>,
): void {
  for (const input of inputs.values()) {
    input.value = "";
  }
}

export function productConfigSelectionKey(keys: readonly string[]): string {
  return [...keys].sort().join("\u0000");
}

export function productConfigRuntimeDraftKey(
  keys: readonly string[],
  values: Readonly<Record<string, string>>,
): string {
  return JSON.stringify(
    [...keys]
      .sort()
      .map((key) => [key, values[key] ?? ""]),
  );
}

export function productConfigDraftLocked(
  planState: BrowserOperationState,
  applyState: BrowserOperationState,
): boolean {
  return (
    [planState.phase, applyState.phase].some((phase) =>
      ["queued", "submitting"].includes(phase),
    ) || applyState.requiresIdempotencyContinuity
  );
}

export function productConfigOperationFailure(error: unknown): BrowserOperationFailure {
  if (error instanceof LaunchplaneApiError) {
    return {
      code: error.code || "request_failed",
      message: error.message,
      statusCode: error.statusCode,
      traceId: error.traceId,
    };
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return {
      code: "request_cancelled",
      message: "The browser stopped waiting for the product configuration operation.",
      statusCode: 0,
      traceId: "",
    };
  }
  return {
    code: "request_failed",
    message: error instanceof Error ? error.message : "Product configuration request failed.",
    statusCode: 0,
    traceId: "",
  };
}

export function productConfigFailureCertainty(
  error: unknown,
  dispatched: boolean,
): "definitive" | "uncertain" {
  if (error instanceof LaunchplaneApiError) {
    return "definitive";
  }
  return dispatched ? "uncertain" : "definitive";
}
