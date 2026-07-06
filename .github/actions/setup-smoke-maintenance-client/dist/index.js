const fs = require("node:fs");
const path = require("node:path");

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

function main() {
  const outputPath = getInput("output-path", {
    defaultValue: ".launchplane/smoke-maintenance-client.mjs",
  });
  const sourcePath = path.join(__dirname, "..", "src", "smoke-maintenance-client.mjs");
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.copyFileSync(sourcePath, outputPath);
  setOutput("client-path", outputPath);
  console.log(`Wrote Launchplane smoke maintenance client to ${outputPath}`);
}

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
