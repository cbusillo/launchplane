import { spawn } from "node:child_process";
import path from "node:path";

const frontendRoot = path.resolve(new URL("..", import.meta.url).pathname);
const defaultSchemaPath = path.join(frontendRoot, "generated", "openapi-ui.json");
const defaultOutputPath = path.join(frontendRoot, "src", "generated", "openapi.ts");

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
  let schemaPath = defaultSchemaPath;
  let outputPath = defaultOutputPath;
  const argv = process.argv.slice(2);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--input") {
      schemaPath = path.resolve(frontendRoot, argv[index + 1]);
      index += 1;
      continue;
    }
    if (argument === "--output") {
      outputPath = path.resolve(frontendRoot, argv[index + 1]);
      index += 1;
    }
  }
  await new Promise((resolve, reject) => {
    const child = spawn(
      "pnpm",
      ["exec", "openapi-ts", "--file", path.join(frontendRoot, "openapi-ts.config.ts")],
      {
        cwd: frontendRoot,
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          LAUNCHPLANE_OPENAPI_INPUT: schemaPath,
          LAUNCHPLANE_OPENAPI_OUTPUT: outputPath,
        },
      },
    );
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
      reject(new Error(stderr || stdout || `pnpm exec openapi-ts exited with code ${code}`));
    });
  });
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
