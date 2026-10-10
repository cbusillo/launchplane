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
  ownerSubmissions: ReadonlyMap<string, string> = new Map(),
): ProductEnvironmentManagedSecretInput[] {
  try {
    return selections.map(({ bindingKey, integration, identity }) => {
      const submissionVersion = ownerSubmissions.get(identity);
      if (submissionVersion) {
        return { binding_key: bindingKey, integration, owner_submission_version_id: submissionVersion };
      }
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

export interface SiteSettingDraft {
  key: string;
  value: string;
}

export interface RuntimeSettingsChange {
  runtime_settings: Record<string, string>;
  retired_provider_keys: string[];
}

// Declared keys, the site's own settings, and provider keys to retire, as one request.
export function productConfigRuntimeChange(
  selectedKeys: readonly string[],
  values: Readonly<Record<string, string>>,
  siteSettings: readonly SiteSettingDraft[],
  retiredKeysText: string,
): RuntimeSettingsChange {
  const entries: [string, string][] = [
    ...selectedKeys.map((key): [string, string] => [key, values[key] ?? ""]),
    ...siteSettings
      .map((setting): [string, string] => [setting.key.trim(), setting.value])
      .filter(([key]) => key),
  ];
  const runtimeSettings: Record<string, string> = {};
  for (const [key, value] of entries.sort(([left], [right]) => left.localeCompare(right))) {
    if (key in runtimeSettings) {
      throw new Error(`${key} appears more than once.`);
    }
    runtimeSettings[key] = value;
  }
  const retiredKeys = [
    ...new Set(retiredKeysText.split(/[\s,]+/).map((key) => key.trim()).filter(Boolean)),
  ].sort();
  return { runtime_settings: runtimeSettings, retired_provider_keys: retiredKeys };
}

export function productConfigRuntimeChangeKey(change: RuntimeSettingsChange): string {
  return JSON.stringify([
    Object.entries(change.runtime_settings),
    change.retired_provider_keys,
  ]);
}

export function productConfigDraftLocked(
  planState: BrowserOperationState,
  applyState: BrowserOperationState,
  reenterOriginalApply = false,
): boolean {
  return (
    [planState.phase, applyState.phase].some((phase) =>
      ["queued", "submitting"].includes(phase),
    ) || (applyState.requiresIdempotencyContinuity && !reenterOriginalApply)
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
  if (error instanceof LaunchplaneApiError && error.statusCode < 500) {
    return "definitive";
  }
  return dispatched ? "uncertain" : "definitive";
}
