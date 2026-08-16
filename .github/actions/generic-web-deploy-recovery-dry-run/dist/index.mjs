const runtime = Reflect.get(globalThis, "process");
const environment = runtime.env;
const requestKeys = new Set([
  "artifact_id",
  "instance",
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

function configureRequestAction() {
  const request = parseRequest(requiredInput("request-json"));
  const product = requestString(request, "product");
  const instance = requestString(request, "instance");
  const artifactId = requestString(request, "artifact_id");
  const sourceGitRef = requestString(request, "source_git_ref");
  const originalRunId = requestPositiveInteger(request, "original_run_id");
  const originalRunAttempt = requestPositiveInteger(request, "original_run_attempt");
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

  environment[environmentKey("launchplane-url")] = requiredInput("launchplane-url");
  environment[environmentKey("route-path")] =
    "/v1/admin/generic-web/deploy-recovery/dry-run";
  environment[environmentKey("payload")] = JSON.stringify(payload);
  environment[environmentKey("idempotency-key")] = idempotencyKey;
  environment[environmentKey("audience")] = input("audience");
  environment[environmentKey("timeout-ms")] = input("timeout-ms", "120000");
  environment[environmentKey("log-response-body")] = "false";
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

try {
  configureRequestAction();
  await import(new URL("../../launchplane-request/dist/index.js", import.meta.url));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  runtime.exitCode = 1;
}
