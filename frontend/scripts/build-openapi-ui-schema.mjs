import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

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
    value.required = Object.entries(value.properties)
      .filter(
        ([, propertySchema]) =>
          !isObject(propertySchema) ||
          propertySchema["x-launchplane-optional-response"] !== true,
      )
      .map(([propertyName]) => propertyName)
      .sort();
  }
  Object.values(value).forEach(requireResponseProperties);
}

function selectUiOperations(openapi, manifestKey, method, selectedPaths) {
  const operations = openapi[manifestKey];
  if (!isObject(operations)) {
    throw new Error(`Canonical OpenAPI is missing ${manifestKey}.`);
  }
  for (const [routePath, operationId] of Object.entries(operations)) {
    if (typeof operationId !== "string") {
      throw new Error(`Canonical OpenAPI has an invalid UI operation id for ${routePath}.`);
    }
    const pathItem = openapi.paths?.[routePath];
    if (!pathItem?.[method]) {
      throw new Error(`Canonical OpenAPI is missing required UI ${method.toUpperCase()} route: ${routePath}`);
    }
    if (pathItem[method].operationId !== operationId) {
      throw new Error(
        `Canonical OpenAPI operation id drift for ${routePath}: expected ${operationId}, got ${pathItem[method].operationId ?? "missing"}.`,
      );
    }
    const selectedPath = selectedPaths[routePath] ?? {};
    if (Object.hasOwn(selectedPath, method)) {
      throw new Error(
        `Canonical OpenAPI selects the UI ${method.toUpperCase()} route more than once: ${routePath}`,
      );
    }
    selectedPaths[routePath] = {
      ...selectedPath,
      ...(pathItem.parameters ? { parameters: pathItem.parameters } : {}),
      [method]: pathItem[method],
    };
  }
}

function collectResponseSchemaReferences(selectedPaths) {
  const found = new Set();
  for (const pathItem of Object.values(selectedPaths)) {
    if (!isObject(pathItem)) {
      continue;
    }
    for (const operation of Object.values(pathItem)) {
      if (!isObject(operation) || !isObject(operation.responses)) {
        continue;
      }
      for (const response of Object.values(operation.responses)) {
        collectSchemaReferences(response, found);
      }
    }
  }
  return found;
}

function hoistInlineSchemaDefinitions(value, inlineSchemas) {
  if (Array.isArray(value)) {
    value.forEach((item) => hoistInlineSchemaDefinitions(item, inlineSchemas));
    return;
  }
  if (!isObject(value)) {
    return;
  }
  if (isObject(value.$defs)) {
    for (const [schemaName, schema] of Object.entries(value.$defs)) {
      const existingSchema = inlineSchemas[schemaName];
      if (existingSchema && JSON.stringify(existingSchema) !== JSON.stringify(schema)) {
        throw new Error(`Canonical OpenAPI defines conflicting inline schema: ${schemaName}`);
      }
      inlineSchemas[schemaName] = schema;
      hoistInlineSchemaDefinitions(schema, inlineSchemas);
    }
    delete value.$defs;
  }
  if (typeof value.$ref === "string" && value.$ref.startsWith("#/$defs/")) {
    value.$ref = `#/components/schemas/${value.$ref.slice("#/$defs/".length)}`;
  }
  Object.values(value).forEach((nestedValue) =>
    hoistInlineSchemaDefinitions(nestedValue, inlineSchemas),
  );
}

function schemaForName(openapi, inlineSchemas, schemaName) {
  return openapi.components?.schemas?.[schemaName] ?? inlineSchemas[schemaName];
}

function collectReferencedSchemas(openapi, inlineSchemas, schemaNames) {
  const queue = [...schemaNames];
  while (queue.length > 0) {
    const schemaName = queue.pop();
    const schema = schemaForName(openapi, inlineSchemas, schemaName);
    if (!schema) {
      throw new Error(`Canonical OpenAPI is missing referenced schema: ${schemaName}`);
    }
    const nestedReferences = collectSchemaReferences(schema);
    for (const nestedName of nestedReferences) {
      if (!schemaNames.has(nestedName)) {
        schemaNames.add(nestedName);
        queue.push(nestedName);
      }
    }
  }
}

function buildUiSchema(openapi) {
  const selectedPaths = {};
  selectUiOperations(
    openapi,
    "x-launchplane-ui-read-operations",
    "get",
    selectedPaths,
  );
  selectUiOperations(
    openapi,
    "x-launchplane-ui-write-operations",
    "post",
    selectedPaths,
  );
  const inlineSchemas = {};
  hoistInlineSchemaDefinitions(selectedPaths, inlineSchemas);

  const selectedSchemaNames = new Set();
  collectSchemaReferences(selectedPaths, selectedSchemaNames);
  collectReferencedSchemas(openapi, inlineSchemas, selectedSchemaNames);

  const selectedSchemas = {};
  for (const schemaName of Array.from(selectedSchemaNames).sort()) {
    selectedSchemas[schemaName] = schemaForName(openapi, inlineSchemas, schemaName);
  }
  const responseSchemaNames = collectResponseSchemaReferences(selectedPaths);
  collectReferencedSchemas(openapi, inlineSchemas, responseSchemaNames);
  const responseSchemas = {};
  for (const schemaName of responseSchemaNames) {
    responseSchemas[schemaName] = schemaForName(openapi, inlineSchemas, schemaName);
  }
  requireResponseProperties(responseSchemas);

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
