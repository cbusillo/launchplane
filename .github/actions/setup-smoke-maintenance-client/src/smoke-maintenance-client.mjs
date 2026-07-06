const SMOKE_MAINTENANCE_ACTIONS = new Set([
  "delete-user",
  "grant-sponsored",
  "promote-owner",
]);

const VERIREEL_SMOKE_MAINTENANCE_INTENTS = new Map([
  ["verireel:delete-user", "owner-route-delete-user"],
  ["verireel:grant-sponsored", "stable-testing-remote-e2e-grant-sponsored"],
  ["verireel:promote-owner", "owner-route-promote-owner"],
  ["verireel-testing:delete-user", "remote-e2e-delete-user"],
  ["verireel-testing:grant-sponsored", "remote-e2e-grant-sponsored"],
]);

function normalizeRequiredText(value, label) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    throw new Error(`${label} is required.`);
  }
  return normalized;
}

function normalizeOptionalText(value) {
  return String(value ?? "").trim();
}

function normalizeSmokeMaintenanceAction(action) {
  const normalizedAction = normalizeRequiredText(action, "Action");
  if (!SMOKE_MAINTENANCE_ACTIONS.has(normalizedAction)) {
    throw new Error(
      `Unsupported Launchplane smoke maintenance action: ${normalizedAction}. Expected delete-user, grant-sponsored, or promote-owner.`,
    );
  }
  return normalizedAction;
}

export function smokeMaintenanceIntentForAction(context, action) {
  const normalizedContext = normalizeRequiredText(context, "Context");
  const normalizedAction = normalizeSmokeMaintenanceAction(action);
  const intent = VERIREEL_SMOKE_MAINTENANCE_INTENTS.get(
    `${normalizedContext}:${normalizedAction}`,
  );
  if (!intent) {
    throw new Error(
      `Unsupported VeriReel smoke maintenance context/action combination: ${normalizedContext}/${normalizedAction}.`,
    );
  }
  return intent;
}

function normalizeSmokeMaintenanceIntent(options, context, action) {
  const derivedIntent = smokeMaintenanceIntentForAction(context, action);
  const explicitIntent = normalizeOptionalText(options.intent);
  if (explicitIntent && explicitIntent !== derivedIntent) {
    throw new Error(
      `VeriReel smoke maintenance intent '${explicitIntent}' does not match derived intent '${derivedIntent}'.`,
    );
  }
  return derivedIntent;
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

function defaultAudienceForBaseUrl(launchplaneBaseUrl) {
  return new URL(launchplaneBaseUrl).host;
}

function buildAbortSignal(timeoutMs, label) {
  const normalizedTimeoutMs = parseNonNegativeInteger(timeoutMs, label);
  if (!normalizedTimeoutMs) {
    return undefined;
  }
  return AbortSignal.timeout(normalizedTimeoutMs);
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

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchWithRetry(url, initOrFactory, options) {
  const attempts = parsePositiveInteger(options.retryAttempts ?? 3, "retryAttempts");
  const delayMs = parseNonNegativeInteger(options.retryDelayMs ?? 250, "retryDelayMs");
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const init = typeof initOrFactory === "function"
        ? await initOrFactory()
        : initOrFactory;
      return await options.fetchImpl(url, init);
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

async function requestGitHubOidcToken(audience, options) {
  const requestUrl = String(options.env.ACTIONS_ID_TOKEN_REQUEST_URL ?? "").trim();
  const requestToken = String(options.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN ?? "").trim();
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
      signal: buildAbortSignal(options.oidcTimeoutMs ?? 30000, "oidcTimeoutMs"),
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

export function buildLaunchplaneSmokeMaintenanceIdempotencyKey(options) {
  const context = normalizeRequiredText(options.context, "Context");
  const action = normalizeSmokeMaintenanceAction(options.action);
  const intent = normalizeSmokeMaintenanceIntent(options, context, action);
  return [
    "product-smoke-maintenance",
    normalizeRequiredText(options.product, "Product"),
    context,
    normalizeRequiredText(options.instance, "Instance"),
    action,
    intent,
    normalizeOptionalText(options.applicationName),
    normalizeOptionalText(options.previewSlug),
    normalizeOptionalText(options.email),
  ].join(":");
}

export function buildLaunchplaneSmokeMaintenanceRequest(options) {
  const context = normalizeRequiredText(options.context, "Context");
  const action = normalizeSmokeMaintenanceAction(options.action);
  const intent = normalizeSmokeMaintenanceIntent(options, context, action);
  const maintenance = {
    schema_version: 1,
    context,
    instance: normalizeRequiredText(options.instance, "Instance"),
    action,
    intent,
    timeout_seconds: parsePositiveInteger(options.timeoutSeconds, "timeoutSeconds"),
  };
  const email = normalizeOptionalText(options.email);
  const applicationName = normalizeOptionalText(options.applicationName);
  const previewSlug = normalizeOptionalText(options.previewSlug);
  if (email) {
    maintenance.email = email;
  }
  if (applicationName) {
    maintenance.application_name = applicationName;
  }
  if (previewSlug) {
    maintenance.preview_slug = previewSlug;
  }

  return {
    routePath: "/v1/drivers/verireel/app-maintenance",
    payload: {
      schema_version: 1,
      product: normalizeRequiredText(options.product, "Product"),
      maintenance,
    },
  };
}

export async function requestLaunchplaneSmokeMaintenance(
  options,
  env = process.env,
  fetchImpl = fetch,
) {
  const launchplaneBaseUrl = normalizeRequiredText(
    options.launchplaneUrl,
    "Launchplane URL",
  );
  const audience = normalizeOptionalText(options.audience)
    || defaultAudienceForBaseUrl(launchplaneBaseUrl);
  const { routePath, payload } = buildLaunchplaneSmokeMaintenanceRequest(options);
  const idempotencyKey = buildLaunchplaneSmokeMaintenanceIdempotencyKey(options);
  const tokenOptions = {
    env,
    fetchImpl,
    oidcTimeoutMs: options.oidcTimeoutMs ?? 30000,
    retryAttempts: options.retryAttempts ?? 3,
    retryDelayMs: options.retryDelayMs ?? 250,
  };
  const token = await requestGitHubOidcToken(audience, tokenOptions);
  const requestUrl = new URL(routePath, launchplaneBaseUrl);
  const response = await fetchWithRetry(
    requestUrl.toString(),
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
      signal: buildAbortSignal(options.timeoutMs ?? 0, "timeoutMs"),
    },
    {
      fetchImpl,
      label: "Launchplane smoke maintenance request",
      retryAttempts: options.retryAttempts ?? 3,
      retryDelayMs: options.retryDelayMs ?? 250,
    },
  );
  const responseText = await response.text();
  let responseBody = {};
  if (!response.ok) {
    throw new Error(
      `Launchplane smoke maintenance request failed with ${response.status}: ${responseText}`,
    );
  }
  if (responseText) {
    responseBody = JSON.parse(responseText);
  }
  const maintenanceStatus = String(responseBody?.result?.maintenance_status ?? "").trim();
  if (["blocked", "fail"].includes(maintenanceStatus)) {
    const message = String(responseBody?.result?.error_message ?? "").trim();
    throw new Error(message || `Launchplane smoke maintenance returned ${maintenanceStatus}.`);
  }
  return {
    audience,
    idempotencyKey,
    payload,
    responseBody,
    routePath,
    statusCode: response.status,
    url: requestUrl.toString(),
  };
}
