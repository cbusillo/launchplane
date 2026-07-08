export const PROTECTED_ARTIFACTS_ROUTE = "/v1/artifacts/protected";
export const PROTECTED_ARTIFACTS_RESPONSE_OUTPUT_PATH = "protected_artifacts";

function requiredText(value, label) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    throw new Error(`${label} is required.`);
  }
  return normalized;
}

function optionalText(value) {
  return String(value ?? "").trim();
}

function queryParameter(name, value) {
  return `${encodeURIComponent(name)}=${encodeURIComponent(value)}`;
}

export function buildProtectedArtifactsRoutePath(options = {}) {
  const product = requiredText(options.product, "Protected artifacts product");
  const context = optionalText(options.context);
  const parameters = [queryParameter("product", product)];
  if (context) {
    parameters.push(queryParameter("context", context));
  }
  return `${PROTECTED_ARTIFACTS_ROUTE}?${parameters.join("&")}`;
}

export function buildProtectedArtifactsRequest(options = {}) {
  return {
    routePath: buildProtectedArtifactsRoutePath(options),
    method: "GET",
    responseOutputPath: PROTECTED_ARTIFACTS_RESPONSE_OUTPUT_PATH,
    responseOutputFile: optionalText(options.responseOutputFile),
  };
}
