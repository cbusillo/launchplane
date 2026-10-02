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

function normalizeRepository(value) {
  return normalizeRequiredText(value, "Repository").toLowerCase();
}

function parsePositiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${label} must be a positive integer.`);
  }
  return parsed;
}

function normalizeSha(sha) {
  const normalized = normalizeRequiredText(sha, "Preview source SHA").toLowerCase();
  if (!/^[0-9a-f]{7,64}$/.test(normalized)) {
    throw new Error("Preview image tags require a hexadecimal commit SHA.");
  }
  return normalized;
}

export function buildPreviewSlugFromPrNumber(prNumber) {
  return `pr-${parsePositiveInteger(prNumber, "PR number")}`;
}

export function buildPreviewImageTags({ prNumber, sha }) {
  const previewSlug = buildPreviewSlugFromPrNumber(prNumber);
  const normalizedSha = normalizeSha(sha);
  return {
    previewSlug,
    floatingTag: previewSlug,
    immutableTag: `${previewSlug}-sha-${normalizedSha}`,
  };
}

export function buildPreviewImageReferences({ imageName, prNumber, sha }) {
  const normalizedImageName = normalizeRequiredText(imageName, "Image name").toLowerCase();
  const imageTags = buildPreviewImageTags({ prNumber, sha });
  return {
    ...imageTags,
    imageName: normalizedImageName,
    floatingImageReference: `${normalizedImageName}:${imageTags.floatingTag}`,
    immutableImageReference: `${normalizedImageName}:${imageTags.immutableTag}`,
  };
}

export function buildSameRepoPreviewPrepareOutputs(options = {}) {
  const event = options.event ?? {};
  const pullRequest = event.pull_request ?? {};
  const action = normalizeOptionalText(options.action ?? event.action);
  // A preview stays up until its PR closes or merges; drafts and labels play no part.
  const currentRepository = normalizeRepository(
    options.currentRepository ?? event.repository?.full_name,
  );
  const headRepository = normalizeRepository(
    options.headRepository ?? pullRequest.head?.repo?.full_name,
  );
  const sameRepo = currentRepository === headRepository;
  const actor = String(options.actor ?? pullRequest.user?.login ?? "")
    .trim()
    .toLowerCase();
  const previewSupported = Boolean(actor) && sameRepo && actor !== "dependabot[bot]";

  let mode = "noop";
  if (["opened", "reopened", "synchronize"].includes(action)) {
    mode = previewSupported ? "refresh" : "unsupported";
  }

  const prNumber = parsePositiveInteger(
    options.prNumber ?? pullRequest.number ?? event.number,
    "PR number",
  );
  const prSha = normalizeSha(options.prSha ?? pullRequest.head?.sha);
  const imageReferences = buildPreviewImageReferences({
    imageName: options.imageName,
    prNumber,
    sha: prSha,
  });
  const runUrl = normalizeOptionalText(options.runUrl);

  return {
    mode,
    same_repo: String(sameRepo),
    preview_supported: String(previewSupported),
    pr_number: String(prNumber),
    pr_sha: prSha,
    image_name: imageReferences.imageName,
    immutable_tag: imageReferences.immutableTag,
    floating_tag: imageReferences.floatingTag,
    immutable_image_reference: imageReferences.immutableImageReference,
    floating_image_reference: imageReferences.floatingImageReference,
    preview_slug: imageReferences.previewSlug,
    run_url: runUrl,
  };
}
