import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import test from "node:test";

const run = promisify(execFile);
const generator = new URL("../scripts/generate-openapi-types.mjs", import.meta.url);

test("client regeneration replaces obsolete exports with the current schema", async () => {
  const scratch = await mkdtemp(path.join(os.tmpdir(), "launchplane-client-test-"));
  try {
    const input = path.join(scratch, "schema.json");
    const output = path.join(scratch, "client");
    for (const schemaName of ["PreviousResponse", "MergedResponse"]) {
      await writeFile(input, JSON.stringify({
        openapi: "3.1.0",
        info: { title: "Generation fixture", version: "test" },
        paths: {},
        components: {
          schemas: {
            [schemaName]: {
              type: "object",
              required: ["message"],
              properties: { message: { type: "string" } },
            },
          },
        },
      }));
      await run(process.execPath, [generator.pathname, "--input", input, "--output", output]);
      const exports = await readFile(path.join(output, "index.ts"), "utf8");
      assert.ok(exports.includes(schemaName), "the current response must be exported");
    }
    const exports = await readFile(path.join(output, "index.ts"), "utf8");
    assert.ok(!exports.includes("PreviousResponse"), "obsolete exports must be removed");
    const types = await readFile(path.join(output, "types.gen.ts"), "utf8");
    assert.ok(!types.includes("PreviousResponse"), "obsolete definitions must be removed");
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
});
