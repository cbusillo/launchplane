import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const frontendRoot = path.resolve(new URL("..", import.meta.url).pathname);
const defaultInputPath = path.join(frontendRoot, "generated", "openapi-canonical.json");
const defaultOutputPath = path.join(frontendRoot, "generated", "openapi-ui.json");

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function collectSchemaReferences(value, found = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) {
      collectSchemaReferences(item, found);
    }
    return found;
  }
  if (!isObject(value)) {
    return found;
  }
  for (const [key, nestedValue] of Object.entries(value)) {
    if (key === "$ref" && typeof nestedValue === "string") {
      const prefix = "#/components/schemas/";
      if (nestedValue.startsWith(prefix)) {
        found.add(nestedValue.slice(prefix.length));
      }
      continue;
    }
    collectSchemaReferences(nestedValue, found);
  }
  return found;
}

function requireResponseProperties(value) {
  if (Array.isArray(value)) {
    value.forEach(requireResponseProperties);
    return;
  }
  if (!isObject(value)) {
    return;
  }
  if (isObject(value.properties)) {
    value.required = Object.keys(value.properties).sort();
  }
  Object.values(value).forEach(requireResponseProperties);
}

function buildUiSchema(openapi) {
  const readOperations = openapi["x-launchplane-ui-read-operations"];
  if (!isObject(readOperations)) {
    throw new Error("Canonical OpenAPI is missing x-launchplane-ui-read-operations.");
  }
  const selectedPaths = {};
  for (const [routePath, operationId] of Object.entries(readOperations)) {
    if (typeof operationId !== "string") {
      throw new Error(`Canonical OpenAPI has an invalid UI operation id for ${routePath}.`);
    }
    const pathItem = openapi.paths?.[routePath];
    if (!pathItem?.get) {
      throw new Error(`Canonical OpenAPI is missing required UI GET route: ${routePath}`);
    }
    if (pathItem.get.operationId !== operationId) {
      throw new Error(
        `Canonical OpenAPI operation id drift for ${routePath}: expected ${operationId}, got ${pathItem.get.operationId ?? "missing"}.`,
      );
    }
    selectedPaths[routePath] = {
      ...(pathItem.parameters ? { parameters: pathItem.parameters } : {}),
      get: pathItem.get,
    };
  }

  const selectedSchemaNames = new Set();
  collectSchemaReferences(selectedPaths, selectedSchemaNames);
  const queue = [...selectedSchemaNames];
  while (queue.length > 0) {
    const schemaName = queue.pop();
    const schema = openapi.components?.schemas?.[schemaName];
    if (!schema) {
      throw new Error(`Canonical OpenAPI is missing referenced schema: ${schemaName}`);
    }
    const nestedReferences = collectSchemaReferences(schema);
    for (const nestedName of nestedReferences) {
      if (!selectedSchemaNames.has(nestedName)) {
        selectedSchemaNames.add(nestedName);
        queue.push(nestedName);
      }
    }
  }

  const selectedSchemas = {};
  for (const schemaName of Array.from(selectedSchemaNames).sort()) {
    selectedSchemas[schemaName] = openapi.components.schemas[schemaName];
  }
  requireResponseProperties(selectedSchemas);

  return {
    openapi: openapi.openapi,
    info: openapi.info,
    paths: selectedPaths,
    components: {
      schemas: selectedSchemas,
    },
  };
}

function parseArgs(argv) {
  let inputPath = defaultInputPath;
  let outputPath = defaultOutputPath;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--input") {
      inputPath = path.resolve(frontendRoot, argv[index + 1]);
      index += 1;
      continue;
    }
    if (argument === "--output") {
      outputPath = path.resolve(frontendRoot, argv[index + 1]);
      index += 1;
    }
  }
  return { inputPath, outputPath };
}

async function main() {
  const { inputPath, outputPath } = parseArgs(process.argv.slice(2));
  const openapi = JSON.parse(await readFile(inputPath, "utf-8"));
  const uiSchema = buildUiSchema(openapi);
  await writeFile(outputPath, `${JSON.stringify(uiSchema, null, 2)}\n`, "utf-8");
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
