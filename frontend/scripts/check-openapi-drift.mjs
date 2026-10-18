import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const repoRoot = path.resolve(new URL("../..", import.meta.url).pathname);
const frontendRoot = path.resolve(new URL("..", import.meta.url).pathname);
const checkedCanonicalPath = path.join(frontendRoot, "generated", "openapi-canonical.json");
const checkedUiSchemaPath = path.join(frontendRoot, "generated", "openapi-ui.json");
const checkedAgentContractPath = path.join(repoRoot, "contracts", "agent-operator-contract.json");

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      reject(new Error(stderr || stdout || `${command} exited with code ${code}`));
    });
  });
}

async function main() {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "launchplane-openapi-drift-"));
  try {
    const regeneratedCanonicalPath = path.join(tempDir, "openapi-canonical.json");
    const regeneratedUiSchemaPath = path.join(tempDir, "openapi-ui.json");
    const regeneratedAgentContractPath = path.join(tempDir, "agent-operator-contract.json");

    await run(
      "uv",
      ["run", "launchplane", "service", "export-openapi", "--output", regeneratedCanonicalPath],
      repoRoot,
    );
    await run(
      "node",
      [
        path.join(frontendRoot, "scripts", "build-openapi-ui-schema.mjs"),
        "--input",
        regeneratedCanonicalPath,
        "--output",
        regeneratedUiSchemaPath,
      ],
      frontendRoot,
    );
    await run(
      "uv",
      ["run", "launchplane", "service", "export-agent-contract", "--output", regeneratedAgentContractPath],
      repoRoot,
    );

    const [
      checkedCanonical,
      regeneratedCanonical,
      checkedUiSchema,
      regeneratedUiSchema,
      checkedAgentContractText,
      regeneratedAgentContractText,
    ] = await Promise.all([
      readFile(checkedCanonicalPath, "utf-8"),
      readFile(regeneratedCanonicalPath, "utf-8"),
      readFile(checkedUiSchemaPath, "utf-8"),
      readFile(regeneratedUiSchemaPath, "utf-8"),
      readFile(checkedAgentContractPath, "utf-8"),
      readFile(regeneratedAgentContractPath, "utf-8"),
    ]);
    const checkedAgentContract = JSON.parse(checkedAgentContractText);
    const regeneratedAgentContract = JSON.parse(regeneratedAgentContractText);
    const agentContractDrifted =
      checkedAgentContract.schema_version !== regeneratedAgentContract.schema_version ||
      checkedAgentContract.normalization_version !== regeneratedAgentContract.normalization_version ||
      checkedAgentContract.semantic_digest_sha256 !== regeneratedAgentContract.semantic_digest_sha256;

    if (
      checkedCanonical !== regeneratedCanonical ||
      checkedUiSchema !== regeneratedUiSchema ||
      agentContractDrifted
    ) {
      throw new Error(
        "Generated contracts are out of date. Run `pnpm --dir frontend generate:openapi` and commit the results.",
      );
    }
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
