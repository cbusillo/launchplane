export const ODOO_PREVIEW_ARTIFACT_PUBLISH_INPUTS_ROUTE =
  "/v1/drivers/odoo/artifact-publish-inputs";
export const ODOO_PREVIEW_APPLY_INPUTS_ROUTE =
  "/v1/drivers/odoo/preview-apply-inputs";
export const ODOO_PREVIEW_APPLY_ROUTE = "/v1/drivers/odoo/preview-apply";

function requiredText(value, label) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    throw new Error(`${label} is required.`);
  }
  return normalized;
}

function optionalText(value) {
  return String(value ?? "").trim();
}

function parsePositiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${label} must be a positive integer.`);
  }
  return parsed;
}

function parseOptionalBoolean(value, defaultValue, label) {
  if (value === undefined || value === null || value === "") {
    return defaultValue;
  }
  if (typeof value === "boolean") {
    return value;
  }
  const normalized = String(value).trim().toLowerCase();
  if (["true", "1", "yes"].includes(normalized)) {
    return true;
  }
  if (["false", "0", "no"].includes(normalized)) {
    return false;
  }
  throw new Error(`${label} must be a boolean.`);
}

function normalizeOperation(value = "refresh") {
  const operation = requiredText(value, "Odoo preview operation").toLowerCase();
  if (!["refresh", "destroy"].includes(operation)) {
    throw new Error("Odoo preview operation must be refresh or destroy.");
  }
  return operation;
}

function normalizeFacts(options = {}) {
  const product = requiredText(options.product, "Odoo preview product");
  const context = requiredText(options.context, "Odoo preview context");
  const instance = requiredText(options.instance ?? "testing", "Odoo preview instance");
  const operation = normalizeOperation(options.operation);
  const prNumber = parsePositiveInteger(options.prNumber, "Odoo preview PR number");
  const sourceGitRef = optionalText(options.sourceGitRef);
  if (operation === "refresh" && !sourceGitRef) {
    throw new Error("Odoo preview refresh requires sourceGitRef.");
  }
  const runId = requiredText(options.runId, "Odoo preview run ID");
  const runAttempt = requiredText(options.runAttempt, "Odoo preview run attempt");
  const source =
    optionalText(options.source) || `${product}-preview-apply-inputs`;
  return {
    product,
    context,
    instance,
    operation,
    prNumber,
    previewSlug: optionalText(options.previewSlug),
    sourceGitRef,
    runId,
    runAttempt,
    source,
  };
}

function previewRequestToken(facts) {
  return facts.previewSlug || `pr-${facts.prNumber}`;
}

function previewSourceToken(facts) {
  if (facts.operation === "destroy") {
    return "destroy";
  }
  return facts.sourceGitRef;
}

function requestEnvelope({
  routePath,
  payload,
  idempotencyKey,
  payloadJsonFiles = {},
  responseOutputPath = "",
  failResultPaths = [],
}) {
  const normalizedRoutePath = requiredText(routePath, "Launchplane route path");
  const normalizedIdempotencyKey = requiredText(
    idempotencyKey,
    "Launchplane idempotency key",
  );
  const normalizedPayloadJsonFiles = Object.fromEntries(
    Object.entries(payloadJsonFiles).map(([payloadPath, filePath]) => [
      requiredText(payloadPath, "Odoo preview payload JSON binding path"),
      requiredText(filePath, "Odoo preview payload JSON binding file"),
    ]),
  );
  return {
    routePath: normalizedRoutePath,
    payload,
    payloadInput: JSON.stringify(payload),
    payloadJsonFiles: normalizedPayloadJsonFiles,
    payloadJsonFilesInput: Object.entries(normalizedPayloadJsonFiles)
      .map(([payloadPath, filePath]) => `${payloadPath}=${filePath}`)
      .join("\n"),
    responseOutputPath: optionalText(responseOutputPath),
    failResultPaths,
    failResultPathsInput: failResultPaths
      .map((path) => requiredText(path, "Odoo preview fail-result path"))
      .join(","),
    idempotencyKey: normalizedIdempotencyKey,
  };
}

export function buildOdooPreviewArtifactPublishInputsRequest(options = {}) {
  const facts = normalizeFacts({ ...options, operation: options.operation ?? "refresh" });
  if (facts.operation !== "refresh") {
    throw new Error(
      "Odoo preview artifact publish inputs require refresh operation.",
    );
  }
  return requestEnvelope({
    routePath: ODOO_PREVIEW_ARTIFACT_PUBLISH_INPUTS_ROUTE,
    payload: {
      schema_version: 1,
      product: facts.product,
      inputs: {
        schema_version: 1,
        context: facts.context,
        instance: facts.instance,
        pr_number: facts.prNumber,
        isolated: true,
        source_git_ref: facts.sourceGitRef,
      },
    },
    idempotencyKey:
      "odoo-artifact-publish-inputs:" +
      `${facts.product}:${facts.context}:${facts.instance}:` +
      `preview-${facts.prNumber}-run-${facts.runId}-attempt-${facts.runAttempt}`,
    failResultPaths: ["result.input_status"],
    responseOutputPath: "result",
  });
}

export function buildOdooPreviewApplyInputsRequest(options = {}) {
  const facts = normalizeFacts(options);
  const manifestFile = optionalText(options.manifestFile);
  if (facts.operation === "refresh" && !manifestFile) {
    throw new Error("Odoo preview refresh apply inputs require manifestFile.");
  }
  return requestEnvelope({
    routePath: ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    payload: {
      schema_version: 1,
      product: facts.product,
      inputs: {
        schema_version: 1,
        product: facts.product,
        operation: facts.operation,
        pr_number: facts.prNumber,
        source_git_ref: facts.sourceGitRef,
        source: facts.source,
        timeout_seconds: 300,
        no_cache: false,
      },
    },
    payloadJsonFiles: manifestFile ? { "inputs.manifest": manifestFile } : {},
    idempotencyKey:
      "odoo-preview-apply-inputs:" +
      `${facts.product}:${previewRequestToken(facts)}:` +
      `${previewSourceToken(facts)}:` +
      `inputs-run-${facts.runId}-attempt-${facts.runAttempt}`,
    failResultPaths: ["result.status"],
    responseOutputPath: "result.dry_run_plan",
  });
}

export function buildOdooPreviewApplyRequest(options = {}) {
  const facts = normalizeFacts(options);
  const planId = requiredText(options.planId, "Odoo preview plan ID");
  const dryRunPlanFile = requiredText(
    options.dryRunPlanFile,
    "Odoo preview dry run plan file",
  );
  const manifestFile = optionalText(options.manifestFile);
  if (facts.operation === "refresh" && !manifestFile) {
    throw new Error("Odoo preview refresh apply requires manifestFile.");
  }
  const payloadJsonFiles = { "apply.dry_run_plan": dryRunPlanFile };
  if (manifestFile) {
    payloadJsonFiles["apply.manifest"] = manifestFile;
  }
  const waitForDeploy = parseOptionalBoolean(
    options.waitForDeploy,
    true,
    "Odoo preview waitForDeploy",
  );
  const applyPayload = {
    timeout_seconds: 600,
    wait_for_deploy: waitForDeploy,
  };
  if (options.smokeCheck !== undefined && options.smokeCheck !== null && options.smokeCheck !== "") {
    applyPayload.smoke_check = parseOptionalBoolean(
      options.smokeCheck,
      false,
      "Odoo preview smokeCheck",
    );
  }
  return requestEnvelope({
    routePath: ODOO_PREVIEW_APPLY_ROUTE,
    payload: {
      schema_version: 1,
      product: facts.product,
      apply: applyPayload,
    },
    payloadJsonFiles,
    idempotencyKey: planId,
    failResultPaths: ["result.status"],
  });
}
