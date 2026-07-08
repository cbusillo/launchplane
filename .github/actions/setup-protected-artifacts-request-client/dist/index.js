const fs = require("node:fs");
const path = require("node:path");
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

function parseBooleanInput(name, options = {}) {
  const value = getInput(name, options);
  if (!value) {
    return false;
  }
  const normalized = value.toLowerCase();
  if (["true", "1", "yes"].includes(normalized)) {
    return true;
  }
  if (["false", "0", "no"].includes(normalized)) {
    return false;
  }
  throw new Error(`${name} must be a boolean.`);
}

function setOutput(name, value) {
  const outputPath = String(process.env.GITHUB_OUTPUT ?? "").trim();
  if (!outputPath) {
    console.warn(`GITHUB_OUTPUT is not set; skipped ${name} output.`);
    return;
  }
  fs.appendFileSync(outputPath, `${name}=${value}\n`, "utf8");
}

async function renderRequest(clientPath) {
  const client = await import(pathToFileURL(clientPath).href);
  return client.buildProtectedArtifactsRequest({
    product: getInput("product", { required: true }),
    context: getInput("context"),
    responseOutputFile: getInput("response-output-file"),
  });
}

function writeRequestOutputs(request) {
  setOutput("route-path", request.routePath);
  setOutput("method", request.method);
  setOutput("response-output-path", request.responseOutputPath);
  setOutput("response-output-file", request.responseOutputFile);
}

async function main() {
  const outputPath = getInput("output-path", {
    defaultValue: ".launchplane/protected-artifacts-request-client.mjs",
  });
  const sourcePath = path.join(
    __dirname,
    "..",
    "src",
    "protected-artifacts-request-client.mjs",
  );
  if (!fs.existsSync(sourcePath)) {
    throw new Error(
      `Protected artifacts request client source is missing at ${sourcePath}.`,
    );
  }
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.copyFileSync(sourcePath, outputPath);
  setOutput("client-path", outputPath);
  console.log(`Wrote Launchplane protected artifacts request client to ${outputPath}`);

  if (parseBooleanInput("render-request", { defaultValue: "false" })) {
    writeRequestOutputs(await renderRequest(outputPath));
    console.log("Rendered Launchplane protected artifacts request");
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
