import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import test from "node:test";

const run = promisify(execFile);
const generator = new URL("../scripts/generate-openapi-types.mjs", import.meta.url);

test("client regeneration preserves concurrent readers and replaces obsolete output", async () => {
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

    const obsoleteFile = path.join(output, "obsolete.ts");
    await writeFile(obsoleteFile, "export type Obsolete = never;\n");
    let complete = false;
    const regenerating = Promise.all([
      run(process.execPath, [generator.pathname, "--input", input, "--output", output]),
      run(process.execPath, [generator.pathname, "--input", input, "--output", output]),
    ])
      .finally(() => { complete = true; });
    let readError;
    while (!complete) {
      try {
        assert.equal(await readFile(path.join(output, "index.ts"), "utf8"), exports);
        assert.equal(await readFile(path.join(output, "types.gen.ts"), "utf8"), types);
      } catch (error) {
        readError = error;
        break;
      }
    }
    await regenerating;
    if (readError) throw readError;
    await assert.rejects(readFile(obsoleteFile), { code: "ENOENT" });
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
});

test("inline definitions normalize references before comparison and reject different schemas", async () => {
  const scratch = await mkdtemp(path.join(os.tmpdir(), "launchplane-inline-schema-"));
  try {
    const input = path.join(scratch, "schema.json");
    const output = path.join(scratch, "ui.json");
    const root = { type: "object", properties: { child: { $ref: "#/$defs/Leaf" } } };
    const schema = {
      openapi: "3.1.0", info: { title: "Inline schemas", version: "test" },
      "x-launchplane-ui-read-operations": {},
      "x-launchplane-ui-write-operations": { "/first": "first", "/second": "second" },
      paths: Object.fromEntries(["first", "second"].map(name => [`/${name}`, { post: {
        operationId: name, responses: {}, requestBody: { content: { "application/json": {
          schema: { $defs: { Root: structuredClone(root), Leaf: { type: "string" } }, $ref: "#/$defs/Root" },
        } } },
      } }])),
    };
    const builder = new URL("../scripts/build-openapi-ui-schema.mjs", import.meta.url);
    await writeFile(input, JSON.stringify(schema));
    await run(process.execPath, [builder.pathname, "--input", input, "--output", output]);
    const ui = JSON.parse(await readFile(output, "utf8"));
    assert.equal(ui.components.schemas.Root.properties.child.$ref, "#/components/schemas/Leaf");
    schema.paths["/second"].post.requestBody.content["application/json"].schema.$defs.Leaf.type = "number";
    await writeFile(input, JSON.stringify(schema));
    await assert.rejects(run(process.execPath, [builder.pathname, "--input", input, "--output", output]));
  } finally { await rm(scratch, { recursive: true, force: true }); }
});
