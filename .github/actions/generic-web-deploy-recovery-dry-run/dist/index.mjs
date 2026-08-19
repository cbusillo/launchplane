import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs";
import { basename, dirname } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

import { downloadArtifact } from "./download-artifact.mjs";

const runtime = Reflect.get(globalThis, "process");
const environment = runtime.env;
const artifactRequestKeys = new Set([
  "artifact_id",
  "expected_recovery_digest",
  "instance",
  "original_run_attempt",
  "original_run_id",
  "product",
  "reason",
  "schema_version",
  "source_git_ref",
]);
const requestKeys = new Set([
  "artifact_id",
  "expected_recovery_digest",
  "instance",
  "launchplane_url",
  "original_run_attempt",
  "original_run_id",
  "product",
  "reason",
  "schema_version",
  "source_git_ref",
]);

function environmentKey(name) {
  return `INPUT_${name.replaceAll(" ", "_").toUpperCase()}`;
}

function input(name, defaultValue = "") {
  return String(environment[environmentKey(name)] ?? defaultValue).trim();
}

function requiredInput(name) {
  const value = input(name);
  if (!value) {
    throw new Error(`${name} is required.`);
  }
  return value;
}

function requestString(request, name) {
  const value = request[name];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`request-json.${name} must be a non-empty string.`);
  }
  return value.trim();
}

function requestPositiveInteger(request, name) {
  const value = requestString(request, name);
  if (!/^[1-9][0-9]*$/.test(value)) {
    throw new Error(`request-json.${name} must be a positive integer string.`);
  }
  return value;
}

function optionalRequestDigest(request) {
  const value = request.expected_recovery_digest;
  if (value === undefined) {
    return "";
  }
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value.trim())) {
    throw new Error(
      "request-json.expected_recovery_digest must be a lowercase SHA-256 digest.",
    );
  }
  return value.trim();
}

function parseRequest(value) {
  let request;
  try {
    request = JSON.parse(value);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`request-json must be valid JSON: ${detail}`);
  }
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("request-json must be a JSON object.");
  }
  const unexpectedKeys = Object.keys(request).filter(key => !requestKeys.has(key));
  if (unexpectedKeys.length > 0) {
    throw new Error(
      `request-json contains unsupported fields: ${unexpectedKeys.sort().join(", ")}.`,
    );
  }
  if (request.schema_version !== 1) {
    throw new Error("request-json.schema_version must equal 1.");
  }
  return request;
}

function validateWorkflowRunEvent(workflowRunId) {
  if (environment.GITHUB_EVENT_NAME !== "workflow_run") {
    throw new Error("Artifact-backed recovery apply requires a workflow_run event.");
  }
  const eventPath = String(environment.GITHUB_EVENT_PATH ?? "").trim();
  if (!eventPath) {
    throw new Error("GITHUB_EVENT_PATH is required for artifact-backed recovery apply.");
  }
  const event = JSON.parse(readFileSync(eventPath, "utf8"));
  const workflowRun = event.workflow_run;
  const repository = String(environment.GITHUB_REPOSITORY ?? "").trim();
  if (
    !workflowRun ||
    String(workflowRun.id) !== workflowRunId ||
    workflowRun.name !== "Launchplane Recovery Apply Request" ||
    workflowRun.path !== ".github/workflows/launchplane-recovery-apply-request.yml" ||
    workflowRun.conclusion !== "success" ||
    workflowRun.event !== "workflow_dispatch" ||
    workflowRun.head_branch !== "main" ||
    !/^[0-9a-f]{40}$/.test(String(workflowRun.head_sha ?? "")) ||
    workflowRun.head_repository?.full_name !== repository
  ) {
    throw new Error("Recovery apply source workflow provenance is invalid.");
  }
}

async function loadRequest() {
  const explicitRequest = input("request-json");
  if (explicitRequest) {
    const request = parseRequest(explicitRequest);
    if (optionalRequestDigest(request)) {
      throw new Error("Digest-bound recovery apply requires workflow-run artifact provenance.");
    }
    return request;
  }

  const workflowRunId = requiredInput("workflow-run-id");
  if (!/^[1-9][0-9]*$/.test(workflowRunId)) {
    throw new Error("workflow-run-id must be a positive integer.");
  }
  validateWorkflowRunEvent(workflowRunId);

  const requestFile = await downloadArtifact(requiredInput("github-token"), workflowRunId);
  const requestDirectory = dirname(requestFile);
  const entries = readdirSync(requestDirectory, { withFileTypes: true });
  if (
    entries.length !== 1 ||
    entries[0].name !== basename(requestFile) ||
    !entries[0].isFile()
  ) {
    throw new Error("Recovery apply artifact must contain exactly one regular file.");
  }
  const requestFileStat = lstatSync(requestFile);
  if (!requestFileStat.isFile() || requestFileStat.isSymbolicLink()) {
    throw new Error("Recovery apply artifact request file is invalid.");
  }
  if (requestFileStat.size === 0 || requestFileStat.size > 8192) {
    throw new Error("Recovery apply artifact exceeds the size limit.");
  }

  const request = parseRequest(readFileSync(requestFile, "utf8"));
  const requestKeyList = Object.keys(request).sort();
  const expectedKeyList = [...artifactRequestKeys].sort();
  if (JSON.stringify(requestKeyList) !== JSON.stringify(expectedKeyList)) {
    throw new Error("Recovery apply artifact has an invalid schema.");
  }

  const product = requestString(request, "product");
  const instance = requestString(request, "instance");
  if (product !== requiredInput("expected-product") || instance !== requiredInput("expected-instance")) {
    throw new Error("Recovery apply target does not match the expected product and instance.");
  }
  if (!/^[^\s]+@sha256:[0-9a-f]{64}$/.test(requestString(request, "artifact_id"))) {
    throw new Error("Recovery apply artifact identity must be immutable.");
  }
  if (!/^[0-9a-f]{40}$/.test(requestString(request, "source_git_ref"))) {
    throw new Error("Recovery apply source commit is invalid.");
  }
  requestPositiveInteger(request, "original_run_id");
  requestPositiveInteger(request, "original_run_attempt");
  if (!optionalRequestDigest(request)) {
    throw new Error("Recovery apply artifact must include a reviewed recovery digest.");
  }
  if (requestString(request, "reason").length > 1000) {
    throw new Error("Recovery apply reason is too long.");
  }
  return request;
}

function readGitHubOutputs() {
  const outputPath = String(environment.GITHUB_OUTPUT ?? "").trim();
  if (!outputPath) {
    throw new Error("GITHUB_OUTPUT is required to verify recovery apply evidence.");
  }
  const lines = readFileSync(outputPath, "utf8").split(/\r?\n/);
  const outputs = new Map();
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const heredocIndex = line.indexOf("<<");
    if (heredocIndex > 0) {
      const name = line.slice(0, heredocIndex);
      const delimiter = line.slice(heredocIndex + 2);
      const values = [];
      index += 1;
      while (index < lines.length && lines[index] !== delimiter) {
        values.push(lines[index]);
        index += 1;
      }
      outputs.set(name, values.join("\n"));
      continue;
    }
    const equalsIndex = line.indexOf("=");
    if (equalsIndex > 0) {
      outputs.set(line.slice(0, equalsIndex), line.slice(equalsIndex + 1));
    }
  }
  return outputs;
}

function verifyApplyOutputs(outputs, expectedRecoveryDigest) {
  const reservationAttempt = outputs.get("reservation_attempt") ?? "";
  const traceId = outputs.get("trace_id") ?? "";
  if (
    outputs.get("status") !== "accepted" ||
    outputs.get("mode") !== "apply" ||
    outputs.get("reservation_state") !== "completed" ||
    outputs.get("recovery_action") !== "adopt_observed" ||
    outputs.get("recovery_digest") !== expectedRecoveryDigest ||
    outputs.get("provider_outcome") !== "present" ||
    outputs.get("provider_status") !== "done" ||
    outputs.get("retry_safe") !== "false" ||
    !/^[1-9][0-9]*$/.test(reservationAttempt) ||
    !traceId.trim()
  ) {
    throw new Error("Launchplane recovery apply did not return adoption-only evidence.");
  }
}

async function waitForApplyOutputs(expectedRecoveryDigest) {
  const outputPath = String(environment.GITHUB_OUTPUT ?? "").trim();
  const timeoutMilliseconds = Number.parseInt(input("timeout-ms", "120000"), 10);
  const deadline = Date.now() + (Number.isFinite(timeoutMilliseconds) ? timeoutMilliseconds : 120000);
  const requiredOutputs = [
    "status",
    "mode",
    "trace_id",
    "reservation_state",
    "reservation_attempt",
    "recovery_action",
    "recovery_digest",
    "provider_outcome",
    "provider_status",
    "retry_safe",
  ];
  while (Date.now() <= deadline) {
    if (runtime.exitCode) {
      throw new Error("Launchplane recovery apply request failed before evidence was available.");
    }
    if (outputPath && existsSync(outputPath)) {
      const outputs = readGitHubOutputs();
      if (requiredOutputs.every(name => outputs.has(name))) {
        verifyApplyOutputs(outputs, expectedRecoveryDigest);
        if (runtime.exitCode) {
          throw new Error("Launchplane recovery apply request failed after producing evidence.");
        }
        return;
      }
    }
    await sleep(50);
  }
  throw new Error("Timed out waiting for Launchplane recovery apply evidence.");
}

function configureRequestAction(request) {
  const launchplaneUrl = input("launchplane-url") || requestString(request, "launchplane_url");
  const product = requestString(request, "product");
  const instance = requestString(request, "instance");
  const artifactId = requestString(request, "artifact_id");
  const sourceGitRef = requestString(request, "source_git_ref");
  const originalRunId = requestPositiveInteger(request, "original_run_id");
  const originalRunAttempt = requestPositiveInteger(request, "original_run_attempt");
  const expectedRecoveryDigest = optionalRequestDigest(request);
  const reason = requestString(request, "reason");
  const idempotencyKey = [
    "generic-web-stable-deploy",
    product,
    instance,
    originalRunId,
    originalRunAttempt,
  ].join(":");
  const payload = {
    schema_version: 1,
    product,
    instance,
    original_deploy: {
      schema_version: 1,
      product,
      deploy: {
        schema_version: 1,
        product,
        instance,
        artifact_id: artifactId,
        source_git_ref: sourceGitRef,
      },
    },
    reason,
  };
  if (expectedRecoveryDigest) {
    payload.expected_recovery_digest = expectedRecoveryDigest;
  }

  environment[environmentKey("launchplane-url")] = launchplaneUrl;
  environment[environmentKey("route-path")] = expectedRecoveryDigest
    ? "/v1/admin/generic-web/deploy-recovery/apply"
    : "/v1/admin/generic-web/deploy-recovery/dry-run";
  environment[environmentKey("payload")] = JSON.stringify(payload);
  environment[environmentKey("idempotency-key")] = idempotencyKey;
  environment[environmentKey("audience")] = input("audience");
  environment[environmentKey("timeout-ms")] = input("timeout-ms", "120000");
  environment[environmentKey("log-response-body")] = "false";
  if (expectedRecoveryDigest) {
    environment[environmentKey("expected-status")] = "202";
    environment[environmentKey("fail-result-paths")] = "";
    environment[environmentKey("retry-attempts")] = "3";
    environment[environmentKey("output-paths")] = [
      "status=status",
      "mode=mode",
      "trace_id=trace_id",
      "reservation_state=reservation_state",
      "reservation_attempt=reservation_attempt",
      "recovery_action=recovery_action",
      "recovery_digest=recovery_digest",
      "provider_outcome=provider_outcome",
      "provider_status=provider_status",
      "retry_safe=retry_safe",
    ].join(",");
  } else {
    environment[environmentKey("output-paths")] = [
      "recovery_digest=recovery_digest",
      "proposed_action=proposed_action",
      "reservation_state=reservation_state",
      "provider_outcome=provider_outcome",
      "provider_status=provider_status",
      "retry_safe=retry_safe",
      "observed_at=observed_at",
    ].join(",");
  }
  return expectedRecoveryDigest;
}

try {
  const expectedRecoveryDigest = configureRequestAction(await loadRequest());
  await import(new URL("../../launchplane-request/dist/index.js", import.meta.url));
  if (expectedRecoveryDigest) {
    await waitForApplyOutputs(expectedRecoveryDigest);
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  runtime.exitCode = 1;
}
