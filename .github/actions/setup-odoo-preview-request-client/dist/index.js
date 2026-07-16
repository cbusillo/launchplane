const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { pathToFileURL } = require("node:url");

function inputNameToEnvKey(name) {
  return `INPUT_${name.replace(/ /g, "_").toUpperCase()}`;
}

function getInput(name, options = {}) {
  const value = String(
    process.env[inputNameToEnvKey(name)] ?? options.defaultValue ?? "",
  ).trim();
  if (options.required && !value) {
    throw new Error(`${name} is required.`);
  }
  return value;
}

function setOutput(name, value) {
  const outputPath = String(process.env.GITHUB_OUTPUT ?? "").trim();
  if (!outputPath) {
    return;
  }
  fs.appendFileSync(outputPath, `${name}=${value}\n`, "utf8");
}

function setMultilineOutput(name, value) {
  const outputPath = String(process.env.GITHUB_OUTPUT ?? "").trim();
  if (!outputPath) {
    return;
  }
  let delimiter = `odoo_preview_request_${randomUUID()}`;
  while (String(value).includes(delimiter)) {
    delimiter = `odoo_preview_request_${randomUUID()}`;
  }
  fs.appendFileSync(outputPath, `${name}<<${delimiter}\n${value}\n${delimiter}\n`, "utf8");
}

function normalizeRequestKind(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (
    normalized &&
    !["artifact-publish-inputs", "preview-apply-inputs", "preview-apply"].includes(normalized)
  ) {
    throw new Error(
      "request-kind must be artifact-publish-inputs, preview-apply-inputs, or preview-apply.",
    );
  }
  return normalized;
}

function requestOptions() {
  return {
    product: getInput("product"),
    context: getInput("context"),
    instance: getInput("instance", { defaultValue: "testing" }),
    operation: getInput("operation", { defaultValue: "refresh" }),
    prNumber: getInput("pr-number"),
    previewSlug: getInput("preview-slug"),
    sourceGitRef: getInput("source-git-ref"),
    source: getInput("source"),
    manifestFile: getInput("manifest-file"),
    dryRunPlanFile: getInput("dry-run-plan-file"),
    planId: getInput("plan-id"),
    waitForDeploy: getInput("wait-for-deploy"),
    smokeCheck: getInput("smoke-check"),
    runId: getInput("run-id", { defaultValue: process.env.GITHUB_RUN_ID ?? "" }),
    runAttempt: getInput("run-attempt", {
      defaultValue: process.env.GITHUB_RUN_ATTEMPT ?? "",
    }),
  };
}

async function renderRequest(kind, clientPath) {
  const client = await import(pathToFileURL(clientPath).href);
  const options = requestOptions();
  if (kind === "artifact-publish-inputs") {
    return client.buildOdooPreviewArtifactPublishInputsRequest(options);
  }
  if (kind === "preview-apply-inputs") {
    return client.buildOdooPreviewApplyInputsRequest(options);
  }
  return client.buildOdooPreviewApplyRequest(options);
}

function writeRequestOutputs(request) {
  setOutput("route-path", request.routePath);
  setMultilineOutput("payload", request.payloadInput);
  setMultilineOutput("payload-json-files", request.payloadJsonFilesInput);
  setOutput("idempotency-key", request.idempotencyKey);
  setOutput("fail-result-paths", request.failResultPathsInput);
  setOutput("response-output-path", request.responseOutputPath);
}

async function main() {
  const outputPath = getInput("output-path", {
    defaultValue: ".launchplane/odoo-preview-request-client.mjs",
  });
  const sourcePath = path.join(__dirname, "..", "src", "odoo-preview-request-client.mjs");
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.copyFileSync(sourcePath, outputPath);
  setOutput("client-path", outputPath);
  console.log(`Wrote Launchplane Odoo preview request client to ${outputPath}`);

  const requestKind = normalizeRequestKind(getInput("request-kind"));
  if (requestKind) {
    writeRequestOutputs(await renderRequest(requestKind, outputPath));
    console.log(`Rendered Launchplane Odoo preview ${requestKind} request`);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
