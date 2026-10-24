import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const repoRoot = path.resolve(new URL("../..", import.meta.url).pathname);
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
    const regeneratedAgentContractPath = path.join(tempDir, "agent-operator-contract.json");
    await run(
      "uv",
      ["run", "launchplane", "service", "export-agent-contract", "--output", regeneratedAgentContractPath],
      repoRoot,
    );

    const [checkedAgentContractText, regeneratedAgentContractText] = await Promise.all([
      readFile(checkedAgentContractPath, "utf-8"),
      readFile(regeneratedAgentContractPath, "utf-8"),
    ]);
    const checkedAgentContract = JSON.parse(checkedAgentContractText);
    const regeneratedAgentContract = JSON.parse(regeneratedAgentContractText);
    const agentContractDrifted =
      checkedAgentContract.schema_version !== regeneratedAgentContract.schema_version ||
      checkedAgentContract.normalization_version !== regeneratedAgentContract.normalization_version ||
      checkedAgentContract.semantic_digest_sha256 !== regeneratedAgentContract.semantic_digest_sha256;

    if (agentContractDrifted) {
      throw new Error(
        "Agent contract is out of date. Run `(cd frontend && pnpm generate:agent-contract)` and commit the result.",
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
