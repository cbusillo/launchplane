import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// Browser smoke evidence lives outside the checkout so finished worktrees
// hold no ignored output. CI points this at a workspace path to upload it.
export const artifactDirVariable = "BROWSER_SMOKE_ARTIFACT_DIR";
const ownerMarker = ".launchplane-browser-smoke";

export function resolveBrowserSmokeArtifactDir({ repoRoot, env = process.env, tempRoot = tmpdir() }) {
  const override = env[artifactDirVariable];
  if (override) {
    // Relative values resolve against the checkout, so CI's value also works
    // inside the browser container, where the checkout is mounted elsewhere.
    return resolve(repoRoot, override);
  }
  const checkout = createHash("sha256").update(repoRoot).digest("hex").slice(0, 12);
  return join(tempRoot, "launchplane-browser-smoke", checkout);
}

// Clear prior evidence, but only from a folder a smoke run created. A
// mistaken override such as the checkout or a shared temp folder is refused.
export function resetBrowserSmokeArtifactDir(directory) {
  let entries;
  try {
    entries = readdirSync(directory);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    entries = [];
  }
  if (entries.length && !entries.includes(ownerMarker)) {
    throw new Error(
      `Refusing to clear ${directory}: it holds files a browser smoke run did not create. ` +
      `Point ${artifactDirVariable} at a new or empty folder.`,
    );
  }
  rmSync(directory, { force: true, recursive: true });
  mkdirSync(directory, { recursive: true });
  writeFileSync(join(directory, ownerMarker), "Browser smoke evidence; each run replaces this folder.\n");
}

const repoRoot = realpathSync(fileURLToPath(new URL("../", import.meta.url)));

export const browserSmokeArtifactDir = resolveBrowserSmokeArtifactDir({ repoRoot });

export function smokeArtifact(...segments) {
  return join(browserSmokeArtifactDir, ...segments);
}
