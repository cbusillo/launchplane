import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const repoRoot = path.resolve(new URL("../..", import.meta.url).pathname);
const frontendRoot = path.resolve(new URL("..", import.meta.url).pathname);
const checkedCanonicalPath = path.join(frontendRoot, "generated", "openapi-canonical.json");
const checkedUiSchemaPath = path.join(frontendRoot, "generated", "openapi-ui.json");
const checkedTypesPath = path.join(frontendRoot, "src", "generated", "openapi.ts");

async function readDirectoryTree(rootPath) {
  const entries = await readdir(rootPath, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const absolutePath = path.join(rootPath, entry.name);
    if (entry.isDirectory()) {
      const nestedFiles = await readDirectoryTree(absolutePath);
      files.push(...nestedFiles.map((file) => path.join(entry.name, file)));
      continue;
    }
    files.push(entry.name);
  }
  return files.sort();
}

async function readDirectoryContents(rootPath) {
  const files = await readDirectoryTree(rootPath);
  const contents = {};
  for (const file of files) {
    contents[file] = await readFile(path.join(rootPath, file), "utf-8");
  }
  return contents;
}

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
    const regeneratedTypesPath = path.join(tempDir, "openapi.ts");

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
      "node",
      [
        path.join(frontendRoot, "scripts", "generate-openapi-types.mjs"),
        "--input",
        regeneratedUiSchemaPath,
        "--output",
        regeneratedTypesPath,
      ],
      frontendRoot,
    );

    const [checkedCanonical, regeneratedCanonical, checkedUiSchema, regeneratedUiSchema, checkedTypes, regeneratedTypes] =
      await Promise.all([
        readFile(checkedCanonicalPath, "utf-8"),
        readFile(regeneratedCanonicalPath, "utf-8"),
        readFile(checkedUiSchemaPath, "utf-8"),
        readFile(regeneratedUiSchemaPath, "utf-8"),
        readDirectoryContents(checkedTypesPath),
        readDirectoryContents(regeneratedTypesPath),
      ]);

    if (
      checkedCanonical !== regeneratedCanonical ||
      checkedUiSchema !== regeneratedUiSchema ||
      JSON.stringify(checkedTypes) !== JSON.stringify(regeneratedTypes)
    ) {
      throw new Error(
        "OpenAPI contracts are out of date. Run `pnpm --dir frontend generate:openapi` and commit the results.",
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
