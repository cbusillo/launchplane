import { Buffer } from "node:buffer";
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import process from "node:process";

const requestFileName = "launchplane-recovery-apply-request.json";

function requiredEnvironment(name) {
  const value = String(process.env[name] ?? "").trim();
  if (!value) {
    throw new Error(`${name} is required.`);
  }
  return value;
}

function positiveRunId(value) {
  if (!/^[1-9][0-9]*$/.test(value)) {
    throw new Error("workflow-run-id must be a positive integer.");
  }
  return value;
}

async function githubRequest(url, token) {
  const response = await fetch(url, {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (!response.ok) {
    throw new Error(`GitHub artifact request failed with ${response.status}.`);
  }
  return response;
}

export async function downloadArtifact(token, workflowRunId) {
  const runId = positiveRunId(workflowRunId);
  const repository = requiredEnvironment("GITHUB_REPOSITORY");
  const apiUrl = requiredEnvironment("GITHUB_API_URL");
  const artifactName = `launchplane-recovery-apply-request-${runId}`;
  const artifactsResponse = await githubRequest(
    `${apiUrl}/repos/${repository}/actions/runs/${runId}/artifacts?per_page=100`,
    token,
  );
  const artifactsPayload = await artifactsResponse.json();
  const artifacts = Array.isArray(artifactsPayload.artifacts) ? artifactsPayload.artifacts : [];
  const matchingArtifacts = artifacts.filter(
    artifact => artifact.name === artifactName && artifact.expired !== true,
  );
  if (matchingArtifacts.length !== 1) {
    throw new Error("Recovery apply source run must contain exactly one matching artifact.");
  }
  const matchingArtifact = matchingArtifacts[0];
  const archiveSize = Reflect.get(matchingArtifact, "size_in_bytes");
  if (!Number.isSafeInteger(archiveSize) || archiveSize <= 0 || archiveSize > 32768) {
    throw new Error("Recovery apply source artifact archive is invalid or too large.");
  }

  const archiveDownloadUrl = String(Reflect.get(matchingArtifact, "archive_download_url") ?? "");
  if (!archiveDownloadUrl) {
    throw new Error("Recovery apply source artifact download URL is missing.");
  }
  const archiveResponse = await githubRequest(archiveDownloadUrl, token);
  const destinationDirectory = join(
    requiredEnvironment("RUNNER_TEMP"),
    `launchplane-recovery-apply-${requiredEnvironment("GITHUB_RUN_ID")}-${requiredEnvironment("GITHUB_RUN_ATTEMPT")}`,
  );
  mkdirSync(destinationDirectory, { recursive: true });
  const archivePath = join(destinationDirectory, "request.zip");
  const archive = Buffer.from(await archiveResponse.arrayBuffer());
  if (archive.length === 0 || archive.length > 32768) {
    throw new Error("Recovery apply downloaded archive is invalid or too large.");
  }
  writeFileSync(archivePath, archive, { mode: 0o600 });
  try {
    const entries = execFileSync("unzip", ["-Z1", archivePath], { encoding: "utf8" })
      .split(/\r?\n/)
      .filter(Boolean);
    if (entries.length !== 1 || entries[0] !== requestFileName) {
      throw new Error("Recovery apply artifact must contain exactly one regular file.");
    }
    const request = execFileSync("unzip", ["-p", archivePath, requestFileName], {
      maxBuffer: 8193,
    });
    if (request.length === 0 || request.length > 8192) {
      throw new Error("Recovery apply artifact exceeds the size limit.");
    }
    const requestPath = join(destinationDirectory, requestFileName);
    writeFileSync(requestPath, request, { mode: 0o600 });
    return requestPath;
  } finally {
    rmSync(archivePath, { force: true });
  }
}
