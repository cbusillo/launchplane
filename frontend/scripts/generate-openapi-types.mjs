import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rename, rm } from "node:fs/promises";
import path from "node:path";

const frontendRoot = path.resolve(new URL("..", import.meta.url).pathname);
const defaultSchemaPath = path.join(frontendRoot, "generated", "openapi-ui.json");
const defaultOutputPath = path.join(frontendRoot, "src", "generated", "openapi.ts");

function run(command, args, cwd, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env,
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

async function generatedFiles(root, prefix = "") {
  const files = [];
  for (const entry of await readdir(path.join(root, prefix), { withFileTypes: true })) {
    const relativePath = path.join(prefix, entry.name);
    if (entry.isDirectory()) {
      files.push(...await generatedFiles(root, relativePath));
    } else {
      files.push(relativePath);
    }
  }
  return files;
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
  await mkdir(path.dirname(outputPath), { recursive: true });
  const stagingPath = await mkdtemp(path.join(path.dirname(outputPath), ".openapi-"));
  try {
    await run(
      "pnpm",
      ["exec", "openapi-ts", "--file", path.join(frontendRoot, "openapi-ts.config.ts")],
      frontendRoot,
      {
        ...process.env,
        LAUNCHPLANE_OPENAPI_INPUT: schemaPath,
        LAUNCHPLANE_OPENAPI_OUTPUT: stagingPath,
      },
    );

    // The generator cleans its destination. Stage privately so a concurrent
    // type check never loses its client while the same schema is regenerated.
    const files = new Set(await generatedFiles(stagingPath));
    await mkdir(outputPath, { recursive: true });
    for (const file of files) {
      const stagedFile = path.join(stagingPath, file);
      const outputFile = path.join(outputPath, file);
      const content = await readFile(stagedFile);
      try {
        if (content.equals(await readFile(outputFile))) continue;
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      await mkdir(path.dirname(outputFile), { recursive: true });
      await rename(stagedFile, outputFile);
    }
    for (const file of await generatedFiles(outputPath)) {
      if (!files.has(file)) await rm(path.join(outputPath, file), { force: true });
    }
  } finally {
    await rm(stagingPath, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
