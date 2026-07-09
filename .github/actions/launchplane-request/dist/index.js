const fs = require("node:fs");
const crypto = require("node:crypto");
const os = require("node:os");

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

function parsePositiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${label} must be a positive integer.`);
  }
  return parsed;
}

function parseNonNegativeInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`${label} must be a non-negative integer.`);
  }
  return parsed;
}

function parseBooleanInput(value, label) {
  const normalizedValue = String(value ?? "").trim().toLowerCase();
  if (["true", "1", "yes", "on"].includes(normalizedValue)) {
    return true;
  }
  if (["false", "0", "no", "off"].includes(normalizedValue)) {
    return false;
  }
  throw new Error(`${label} must be true or false.`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isAbortError(error) {
  return error instanceof DOMException
    ? error.name === "AbortError" || error.name === "TimeoutError"
    : error?.name === "AbortError" || error?.name === "TimeoutError";
}

function describeError(error) {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

async function fetchWithRetry(url, initOrFactory, options) {
  const attempts = parsePositiveInteger(options.retryAttempts, "retry-attempts");
  const delayMs = parseNonNegativeInteger(options.retryDelayMs, "retry-delay-ms");
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const init = typeof initOrFactory === "function"
        ? await initOrFactory()
        : initOrFactory;
      return await fetch(url, init);
    } catch (error) {
      lastError = error;
      if (isAbortError(error) || attempt >= attempts) {
        break;
      }
      await sleep(delayMs * attempt);
    }
  }
  throw new Error(
    `${options.label} failed after ${attempts} attempts: ${describeError(lastError)}`,
  );
}

async function readJsonResponse(response) {
  const responseText = await response.text();
  let responseBody = {};
  if (responseText) {
    try {
      responseBody = JSON.parse(responseText);
    } catch (error) {
      if (response.ok) {
        throw error;
      }
    }
  }
  return { responseBody, responseText };
}

function buildAbortSignal(timeoutMs) {
  const normalizedTimeoutMs = parseNonNegativeInteger(timeoutMs, "timeout-ms");
  if (!normalizedTimeoutMs) {
    return undefined;
  }
  return AbortSignal.timeout(normalizedTimeoutMs);
}

async function requestGitHubOidcToken(audience, options) {
  const requestUrl = String(process.env.ACTIONS_ID_TOKEN_REQUEST_URL ?? "").trim();
  const requestToken = String(process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN ?? "").trim();
  if (!requestUrl || !requestToken) {
    throw new Error(
      "GitHub OIDC request environment is missing. Ensure the workflow job grants id-token: write.",
    );
  }

  const tokenUrl = new URL(requestUrl);
  tokenUrl.searchParams.set("audience", audience);
  const response = await fetchWithRetry(
    tokenUrl.toString(),
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${requestToken}`,
        Accept: "application/json",
      },
      signal: buildAbortSignal(options.oidcTimeoutMs),
    },
    { ...options, label: "GitHub OIDC token request" },
  );
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`GitHub OIDC token request failed with ${response.status}: ${body}`);
  }
  const parsed = body ? JSON.parse(body) : {};
  const token = String(parsed.value ?? "").trim();
  if (!token) {
    throw new Error("GitHub OIDC token response did not include a token value.");
  }
  return token;
}

function readPayload() {
  const inlinePayload = getInput("payload");
  const payloadFile = getInput("payload-file");
  const payloadListFile = getInput("payload-list-file");
  const configuredPayloadInputs = [inlinePayload, payloadFile, payloadListFile].filter(Boolean);
  if (configuredPayloadInputs.length > 1) {
    throw new Error("Use only one of payload, payload-file, or payload-list-file.");
  }
  if (payloadListFile) {
    const payloads = JSON.parse(fs.readFileSync(payloadListFile, "utf8"));
    if (!Array.isArray(payloads)) {
      throw new Error("payload-list-file must contain a JSON array.");
    }
    return { mode: "list", payloads };
  }
  if (inlinePayload) {
    return { mode: "single", payload: JSON.parse(inlinePayload) };
  }
  if (payloadFile) {
    return { mode: "single", payload: JSON.parse(fs.readFileSync(payloadFile, "utf8")) };
  }
  return { mode: "single", payload: null };
}

function parsePayloadFieldValue(value) {
  const normalizedValue = String(value ?? "").trim();
  if (!normalizedValue) {
    return "";
  }
  try {
    return JSON.parse(normalizedValue);
  } catch {
    return String(value ?? "");
  }
}

function writeJsonPath(root, path, value) {
  const segments = path.split(".").map((segment) => segment.trim()).filter(Boolean);
  if (segments.length === 0) {
    throw new Error("Payload field path must not be empty.");
  }
  let cursor = root;
  for (const segment of segments.slice(0, -1)) {
    if (cursor[segment] === undefined) {
      cursor[segment] = {};
    }
    if (cursor[segment] === null || typeof cursor[segment] !== "object" || Array.isArray(cursor[segment])) {
      throw new Error(`Payload field '${path}' cannot descend through non-object segment '${segment}'.`);
    }
    cursor = cursor[segment];
  }
  cursor[segments.at(-1)] = value;
}

function applyPayloadFields(payload) {
  const payloadFields = getInput("payload-fields");
  if (!payloadFields) {
    return payload;
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("payload-fields requires payload or payload-file to contain a JSON object.");
  }
  for (const line of payloadFields.split(/\r?\n/)) {
    const trimmedLine = line.trim();
    if (!trimmedLine || trimmedLine.startsWith("#")) {
      continue;
    }
    const separatorIndex = trimmedLine.indexOf("=");
    if (separatorIndex <= 0) {
      throw new Error(`Invalid payload field '${trimmedLine}'. Use json.path=value.`);
    }
    const path = trimmedLine.slice(0, separatorIndex).trim();
    const value = line.slice(line.indexOf("=") + 1);
    writeJsonPath(payload, path, parsePayloadFieldValue(value));
  }
  return payload;
}

function applyPayloadTransforms(payloadConfig) {
  if (payloadConfig.mode === "list") {
    if (getInput("payload-fields") || getInput("payload-json-files")) {
      throw new Error("payload-list-file cannot be combined with payload-fields or payload-json-files.");
    }
    return payloadConfig;
  }
  return {
    mode: "single",
    payload: applyPayloadJsonFiles(applyPayloadFields(payloadConfig.payload)),
  };
}

function applyPayloadJsonFiles(payload) {
  const payloadJsonFiles = getInput("payload-json-files");
  if (!payloadJsonFiles) {
    return payload;
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("payload-json-files requires payload or payload-file to contain a JSON object.");
  }
  for (const line of payloadJsonFiles.split(/\r?\n/)) {
    const trimmedLine = line.trim();
    if (!trimmedLine || trimmedLine.startsWith("#")) {
      continue;
    }
    const separatorIndex = trimmedLine.indexOf("=");
    if (separatorIndex <= 0) {
      throw new Error(`Invalid payload JSON file '${trimmedLine}'. Use json.path=file-path.`);
    }
    const path = trimmedLine.slice(0, separatorIndex).trim();
    const filePath = line.slice(line.indexOf("=") + 1).trim();
    if (!filePath) {
      throw new Error(`Payload JSON file '${path}' requires a file path.`);
    }
    const fileValue = JSON.parse(fs.readFileSync(filePath, "utf8"));
    writeJsonPath(payload, path, fileValue);
  }
  return payload;
}

function splitCommaSeparated(value) {
  return String(value ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function parseExpectedStatuses(value) {
  const statuses = splitCommaSeparated(value);
  if (statuses.length === 0) {
    return null;
  }
  const expectedStatuses = new Set();
  for (const status of statuses) {
    if (!/^\d{3}$/.test(status)) {
      throw new Error(`expected-status entry '${status}' must be a three-digit HTTP status code.`);
    }
    expectedStatuses.add(Number(status));
  }
  return expectedStatuses;
}

function isExpectedStatus(response, expectedStatuses) {
  if (!expectedStatuses) {
    return response.ok;
  }
  return expectedStatuses.has(response.status);
}

function readJsonPath(value, path) {
  let cursor = value;
  for (const segment of path.split(".").filter(Boolean)) {
    if (cursor === null || typeof cursor !== "object" || !(segment in cursor)) {
      return undefined;
    }
    cursor = cursor[segment];
  }
  return cursor;
}

function setOutput(name, value) {
  const outputPath = String(process.env.GITHUB_OUTPUT ?? "").trim();
  if (!outputPath) {
    return;
  }
  const normalizedValue = String(value ?? "");
  const delimiter = `EOF_${name.toUpperCase().replace(/[^A-Z0-9_]/g, "_")}_${Date.now()}`;
  fs.appendFileSync(
    outputPath,
    `${name}<<${delimiter}${os.EOL}${normalizedValue}${os.EOL}${delimiter}${os.EOL}`,
    "utf8",
  );
}

function writeMappedOutputs(responseBody) {
  const mappings = splitCommaSeparated(getInput("output-paths"));
  for (const mapping of mappings) {
    const [rawName, rawPath] = mapping.split("=");
    const name = String(rawName ?? "").trim();
    const path = String(rawPath ?? "").trim();
    if (!name || !path) {
      throw new Error(`Invalid output mapping '${mapping}'. Use output=json.path.`);
    }
    setOutput(name, readJsonPath(responseBody, path) ?? "");
  }
}

function writeResponseOutputFile(responseBody) {
  const outputFile = getInput("response-output-file");
  if (!outputFile) {
    return;
  }
  const outputPath = getInput("response-output-path");
  const outputValue = outputPath ? readJsonPath(responseBody, outputPath) : responseBody;
  fs.writeFileSync(outputFile, `${JSON.stringify(outputValue ?? null)}\n`, "utf8");
}

function payloadDigest(payload) {
  return crypto
    .createHash("sha256")
    .update(JSON.stringify(payload))
    .digest("hex");
}

function assertResultStatuses(responseBody) {
  const failStatuses = new Set(
    splitCommaSeparated(
      getInput("fail-result-statuses", { defaultValue: "fail,blocked" }),
    ).map((status) => status.toLowerCase()),
  );
  const failResultPaths = splitCommaSeparated(
    getInput("fail-result-paths", {
      defaultValue:
        "result.refresh_status,result.deploy_status,result.rollout_status," +
        "result.migration_status,result.health_status",
    }),
  );
  if (failStatuses.size === 0 || failResultPaths.length === 0) {
    return;
  }
  for (const path of failResultPaths) {
    const value = readJsonPath(responseBody, path);
    const normalizedValue = String(value ?? "").trim().toLowerCase();
    if (normalizedValue && failStatuses.has(normalizedValue)) {
      const message = String(
        readJsonPath(responseBody, "result.error_message") ?? "",
      ).trim();
      throw new Error(message || `Launchplane result ${path} was ${normalizedValue}.`);
    }
  }
}

function getActionOptions() {
  return {
    expectedStatuses: parseExpectedStatuses(getInput("expected-status")),
    oidcTimeoutMs: getInput("oidc-timeout-ms", { defaultValue: "30000" }),
    pollIntervalMs: getInput("poll-interval-ms", { defaultValue: "30000" }),
    pollResultPath: getInput("poll-result-path"),
    pollResultStatuses: getInput("poll-result-statuses", { defaultValue: "pending" }),
    pollTimeoutMs: getInput("poll-timeout-ms", { defaultValue: "600000" }),
    retryAttempts: getInput("retry-attempts", { defaultValue: "3" }),
    retryDelayMs: getInput("retry-delay-ms", { defaultValue: "250" }),
  };
}

function shouldPollResponse(responseBody, options) {
  const pollResultPath = String(options.pollResultPath ?? "").trim();
  if (!pollResultPath) {
    return false;
  }
  const pollStatuses = new Set(
    splitCommaSeparated(options.pollResultStatuses).map((status) =>
      status.toLowerCase(),
    ),
  );
  if (pollStatuses.size === 0) {
    return false;
  }
  const value = readJsonPath(responseBody, pollResultPath);
  const normalizedValue = String(value ?? "").trim().toLowerCase();
  return Boolean(normalizedValue && pollStatuses.has(normalizedValue));
}

async function requestLaunchplaneUntilComplete(requestUrl, requestInit, options) {
  const pollResultPath = String(options.pollResultPath ?? "").trim();
  const pollTimeoutMs = parseNonNegativeInteger(
    options.pollTimeoutMs,
    "poll-timeout-ms",
  );
  const pollIntervalMs = parseNonNegativeInteger(
    options.pollIntervalMs,
    "poll-interval-ms",
  );
  const deadline = Date.now() + pollTimeoutMs;
  let lastPollValue = "";
  let attempt = 0;

  while (true) {
    attempt += 1;
    const response = await fetchWithRetry(requestUrl, requestInit, {
      ...options,
      label: "Launchplane request",
    });
    const { responseBody, responseText } = await readJsonResponse(response);
    if (!response.ok || !shouldPollResponse(responseBody, options)) {
      return { response, responseBody, responseText };
    }

    lastPollValue = String(readJsonPath(responseBody, pollResultPath) ?? "").trim();
    if (!pollTimeoutMs || Date.now() + pollIntervalMs > deadline) {
      throw new Error(
        `Timed out waiting for Launchplane result ${pollResultPath} to leave ` +
          `${lastPollValue || "the polling status"} after ${pollTimeoutMs}ms.`,
      );
    }
    process.stderr.write(
      `Launchplane result ${pollResultPath} is ${lastPollValue}; polling again in ${pollIntervalMs}ms.\n`,
    );
    await sleep(pollIntervalMs);
  }
}

async function main() {
  const launchplaneUrl = new URL(getInput("launchplane-url", { required: true }));
  const routePath = getInput("route-path", { required: true });
  const method = getInput("method", { defaultValue: "POST" });
  const requestUrl = new URL(routePath, launchplaneUrl).toString();
  const audience = getInput("audience") || launchplaneUrl.host;
  const idempotencyKey = getInput("idempotency-key");
  const idempotencyKeyPrefix = getInput("idempotency-key-prefix");
  const logResponseBody = parseBooleanInput(
    getInput("log-response-body", { defaultValue: "true" }),
    "log-response-body",
  );
  const payloadConfig = applyPayloadTransforms(readPayload());
  const options = getActionOptions();
  async function requestInit(payload, requestIdempotencyKey) {
    const token = await requestGitHubOidcToken(audience, options);
    const headers = {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
    };
    const init = {
      method,
      headers,
      signal: buildAbortSignal(getInput("timeout-ms", { defaultValue: "0" })),
    };
    if (requestIdempotencyKey) {
      headers["Idempotency-Key"] = requestIdempotencyKey;
    }
    if (payload !== null) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(payload);
    }
    return init;
  }

  if (payloadConfig.mode === "list") {
    if (idempotencyKey) {
      throw new Error("Use idempotency-key-prefix with payload-list-file, not idempotency-key.");
    }
    if (!idempotencyKeyPrefix) {
      throw new Error("idempotency-key-prefix is required when payload-list-file is set.");
    }
    if (payloadConfig.payloads.length === 0) {
      setOutput("launchplane-url", requestUrl);
      setOutput("route-path", routePath);
      setOutput("audience", audience);
      setOutput("status-code", "");
      setOutput("response-body", "[]");
      writeResponseOutputFile([]);
      if (logResponseBody) {
        process.stdout.write("[]\n");
      }
      return;
    }
    for (const [index, payload] of payloadConfig.payloads.entries()) {
      if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error(`payload-list-file entry ${index + 1} must be a JSON object.`);
      }
    }
    const responseBodies = [];
    let lastStatus = "";
    for (const [index, payload] of payloadConfig.payloads.entries()) {
      const requestIdempotencyKey = `${idempotencyKeyPrefix}:${payloadDigest(payload)}`;
      let response;
      let responseBody;
      let responseText;
      try {
        ({ response, responseBody, responseText } = await requestLaunchplaneUntilComplete(
          requestUrl,
          () => requestInit(payload, requestIdempotencyKey),
          options,
        ));
      } catch (error) {
        if (responseBodies.length > 0) {
          writeResponseOutputFile(responseBodies);
        }
        throw error;
      }
      lastStatus = String(response.status);
      responseBodies.push(responseBody);
      if (!isExpectedStatus(response, options.expectedStatuses)) {
        writeResponseOutputFile(responseBodies);
        const responseDetail = logResponseBody ? `: ${responseText || "empty response"}` : "";
        throw new Error(
          `Launchplane request ${index + 1} failed with ${response.status}${responseDetail}`,
        );
      }
      try {
        assertResultStatuses(responseBody);
      } catch (error) {
        writeResponseOutputFile(responseBodies);
        throw error;
      }
    }
    setOutput("launchplane-url", requestUrl);
    setOutput("route-path", routePath);
    setOutput("audience", audience);
    setOutput("status-code", lastStatus);
    setOutput("response-body", JSON.stringify(responseBodies));
    writeMappedOutputs(responseBodies.at(-1) ?? {});
    writeResponseOutputFile(responseBodies);
    if (logResponseBody) {
      process.stdout.write(`${JSON.stringify(responseBodies, null, 2)}\n`);
    }
    return;
  }

  const payload = payloadConfig.payload;

  const { response, responseBody, responseText } = await requestLaunchplaneUntilComplete(
    requestUrl,
    () => requestInit(payload, idempotencyKey),
    options,
  );

  setOutput("launchplane-url", requestUrl);
  setOutput("route-path", routePath);
  setOutput("audience", audience);
  setOutput("status-code", response.status);
  setOutput("response-body", responseText);
  writeMappedOutputs(responseBody);
  writeResponseOutputFile(responseBody);

  if (logResponseBody) {
    process.stdout.write(`${JSON.stringify(responseBody, null, 2)}\n`);
  }
  if (!isExpectedStatus(response, options.expectedStatuses)) {
    const responseDetail = logResponseBody ? `: ${responseText || "empty response"}` : "";
    throw new Error(
      `Launchplane request failed with ${response.status}${responseDetail}`,
    );
  }
  assertResultStatuses(responseBody);
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
