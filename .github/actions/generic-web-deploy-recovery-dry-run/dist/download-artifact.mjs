import { Buffer } from "node:buffer";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import process from "node:process";
import { inflateRawSync } from "node:zlib";

const requestFileName = "launchplane-recovery-apply-request.json";
const maximumArchiveSize = 32768;
const maximumRequestSize = 8192;
const endOfCentralDirectorySignature = 0x06054b50;
const centralDirectorySignature = 0x02014b50;
const localFileSignature = 0x04034b50;

function crc32(value) {
  let checksum = 0xffffffff;
  for (const byte of value) {
    checksum ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      checksum = checksum >>> 1 ^ 0xedb88320 & -(checksum & 1);
    }
  }
  return (checksum ^ 0xffffffff) >>> 0;
}

function findEndOfCentralDirectory(archive) {
  for (let offset = archive.length - 22; offset >= 0; offset -= 1) {
    if (archive.readUInt32LE(offset) === endOfCentralDirectorySignature) {
      return offset;
    }
  }
  throw new Error("Recovery apply artifact is not a valid ZIP archive.");
}

function extractRequest(archive) {
  const endOffset = findEndOfCentralDirectory(archive);
  const commentLength = archive.readUInt16LE(endOffset + 20);
  if (endOffset + 22 + commentLength !== archive.length) {
    throw new Error("Recovery apply artifact ZIP footer is invalid.");
  }
  const diskNumber = archive.readUInt16LE(endOffset + 4);
  const centralDirectoryDisk = archive.readUInt16LE(endOffset + 6);
  const diskEntries = archive.readUInt16LE(endOffset + 8);
  const totalEntries = archive.readUInt16LE(endOffset + 10);
  const centralDirectorySize = archive.readUInt32LE(endOffset + 12);
  const centralDirectoryOffset = archive.readUInt32LE(endOffset + 16);
  if (
    diskNumber !== 0 ||
    centralDirectoryDisk !== 0 ||
    diskEntries !== 1 ||
    totalEntries !== 1 ||
    centralDirectoryOffset + centralDirectorySize !== endOffset
  ) {
    throw new Error("Recovery apply artifact must contain exactly one regular file.");
  }
  if (
    centralDirectoryOffset + 46 > endOffset ||
    archive.readUInt32LE(centralDirectoryOffset) !== centralDirectorySignature
  ) {
    throw new Error("Recovery apply artifact central directory is invalid.");
  }

  const versionMadeBy = archive.readUInt16LE(centralDirectoryOffset + 4);
  const flags = archive.readUInt16LE(centralDirectoryOffset + 8);
  const compressionMethod = archive.readUInt16LE(centralDirectoryOffset + 10);
  const expectedChecksum = archive.readUInt32LE(centralDirectoryOffset + 16);
  const compressedSize = archive.readUInt32LE(centralDirectoryOffset + 20);
  const uncompressedSize = archive.readUInt32LE(centralDirectoryOffset + 24);
  const fileNameLength = archive.readUInt16LE(centralDirectoryOffset + 28);
  const extraLength = archive.readUInt16LE(centralDirectoryOffset + 30);
  const fileCommentLength = archive.readUInt16LE(centralDirectoryOffset + 32);
  const fileDisk = archive.readUInt16LE(centralDirectoryOffset + 34);
  const externalAttributes = archive.readUInt32LE(centralDirectoryOffset + 38);
  const localHeaderOffset = archive.readUInt32LE(centralDirectoryOffset + 42);
  const centralEntryEnd =
    centralDirectoryOffset + 46 + fileNameLength + extraLength + fileCommentLength;
  const fileName = archive
    .subarray(centralDirectoryOffset + 46, centralDirectoryOffset + 46 + fileNameLength)
    .toString("utf8");
  const unixFileType = externalAttributes >>> 16 & 0o170000;
  if (
    centralEntryEnd !== endOffset ||
    fileDisk !== 0 ||
    fileName !== requestFileName ||
    (flags & 1) !== 0 ||
    ![0, 8].includes(compressionMethod) ||
    uncompressedSize === 0 ||
    uncompressedSize > maximumRequestSize ||
    (externalAttributes & 0x10) !== 0 ||
    versionMadeBy >>> 8 === 3 && unixFileType !== 0 && unixFileType !== 0o100000
  ) {
    throw new Error("Recovery apply artifact must contain exactly one regular file.");
  }

  if (
    localHeaderOffset + 30 > centralDirectoryOffset ||
    archive.readUInt32LE(localHeaderOffset) !== localFileSignature
  ) {
    throw new Error("Recovery apply artifact local file header is invalid.");
  }
  const localFlags = archive.readUInt16LE(localHeaderOffset + 6);
  const localCompressionMethod = archive.readUInt16LE(localHeaderOffset + 8);
  const localFileNameLength = archive.readUInt16LE(localHeaderOffset + 26);
  const localExtraLength = archive.readUInt16LE(localHeaderOffset + 28);
  const dataOffset = localHeaderOffset + 30 + localFileNameLength + localExtraLength;
  const dataEnd = dataOffset + compressedSize;
  const localFileName = archive
    .subarray(localHeaderOffset + 30, localHeaderOffset + 30 + localFileNameLength)
    .toString("utf8");
  if (
    localFileName !== requestFileName ||
    localFlags !== flags ||
    localCompressionMethod !== compressionMethod ||
    dataOffset > centralDirectoryOffset ||
    dataEnd > centralDirectoryOffset
  ) {
    throw new Error("Recovery apply artifact local file header is invalid.");
  }
  const usesDataDescriptor = (flags & 8) !== 0;
  if (!usesDataDescriptor && dataEnd !== centralDirectoryOffset) {
    throw new Error("Recovery apply artifact contains unreferenced ZIP data.");
  }
  if (usesDataDescriptor) {
    const descriptorHasSignature =
      dataEnd + 4 <= centralDirectoryOffset && archive.readUInt32LE(dataEnd) === 0x08074b50;
    const descriptorOffset = dataEnd + (descriptorHasSignature ? 4 : 0);
    if (
      descriptorOffset + 12 !== centralDirectoryOffset ||
      archive.readUInt32LE(descriptorOffset) !== expectedChecksum ||
      archive.readUInt32LE(descriptorOffset + 4) !== compressedSize ||
      archive.readUInt32LE(descriptorOffset + 8) !== uncompressedSize
    ) {
      throw new Error("Recovery apply artifact data descriptor is invalid.");
    }
  }

  const compressedRequest = archive.subarray(dataOffset, dataEnd);
  let request;
  try {
    request =
      compressionMethod === 0
        ? compressedRequest
        : inflateRawSync(compressedRequest, { maxOutputLength: maximumRequestSize + 1 });
  } catch {
    throw new Error("Recovery apply artifact request data is invalid.");
  }
  if (
    request.length !== uncompressedSize ||
    request.length === 0 ||
    request.length > maximumRequestSize ||
    crc32(request) !== expectedChecksum
  ) {
    throw new Error("Recovery apply artifact request data is invalid.");
  }
  return request;
}

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
    `${apiUrl}/repos/${repository}/actions/runs/${runId}/artifacts?name=${encodeURIComponent(artifactName)}&per_page=100`,
    token,
  );
  const artifactsPayload = await artifactsResponse.json();
  const artifacts = Array.isArray(artifactsPayload.artifacts) ? artifactsPayload.artifacts : [];
  const matchingArtifacts = artifacts.filter(
    artifact => artifact.name === artifactName && artifact.expired !== true,
  );
  if (artifactsPayload.total_count !== artifacts.length || matchingArtifacts.length !== 1) {
    throw new Error("Recovery apply source run must contain exactly one matching artifact.");
  }
  const matchingArtifact = matchingArtifacts[0];
  const archiveSize = Reflect.get(matchingArtifact, "size_in_bytes");
  if (!Number.isSafeInteger(archiveSize) || archiveSize <= 0 || archiveSize > maximumArchiveSize) {
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
  rmSync(destinationDirectory, { recursive: true, force: true });
  mkdirSync(destinationDirectory, { recursive: true });
  const archive = Buffer.from(await archiveResponse.arrayBuffer());
  if (archive.length === 0 || archive.length > maximumArchiveSize) {
    throw new Error("Recovery apply downloaded archive is invalid or too large.");
  }
  const request = extractRequest(archive);
  const requestPath = join(destinationDirectory, requestFileName);
  writeFileSync(requestPath, request, { mode: 0o600 });
  return requestPath;
}
