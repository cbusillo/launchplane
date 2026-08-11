import { LaunchplaneApiError } from "./api";
import type {
  OwnerAcceptanceBinding,
  OwnerAcceptanceEventEnvelope,
} from "./generated/openapi.ts";
import type { BrowserOperationFailure } from "./browser-operation";

export type OwnerAcceptanceHumanAction = OwnerAcceptanceEventEnvelope["action"];

export function ownerAcceptanceOperationScope(binding: OwnerAcceptanceBinding): string {
  return [
    "owner-acceptance",
    binding.repository,
    binding.pull_request_number,
    binding.product,
    binding.system,
    binding.action,
    binding.environment,
    binding.binding_sha256,
  ].join(":");
}

export function ownerAcceptanceRequest(
  binding: OwnerAcceptanceBinding,
  action: OwnerAcceptanceHumanAction,
  reason: string,
  resolution: OwnerAcceptanceEventEnvelope["resolution"] = null,
): OwnerAcceptanceEventEnvelope {
  return {
    schema_version: 1,
    target: {
      repository: binding.repository,
      pull_request_number: binding.pull_request_number,
    },
    action,
    expected_binding_sha256: binding.binding_sha256,
    reason: reason.trim(),
    resolution,
  };
}

export function ownerAcceptanceFailure(error: unknown): BrowserOperationFailure {
  if (error instanceof LaunchplaneApiError) {
    return {
      code: error.code,
      message: error.message,
      statusCode: error.statusCode,
      traceId: error.traceId,
    };
  }
  return {
    code: "owner_acceptance_request_failed",
    message: error instanceof Error ? error.message : "Owner acceptance request failed.",
    statusCode: 0,
    traceId: "",
  };
}

export function ownerAcceptanceFailureCertainty(
  error: unknown,
  dispatched: boolean,
): "definitive" | "uncertain" {
  if (!dispatched) return "definitive";
  if (!(error instanceof LaunchplaneApiError)) return "uncertain";
  return error.statusCode === 408 || error.statusCode === 429 || error.statusCode >= 500
    ? "uncertain"
    : "definitive";
}
