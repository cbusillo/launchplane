import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// Browser smoke evidence lives outside the checkout so finished worktrees
// hold no ignored output. CI points this at a workspace path to upload it.
export const artifactDirVariable = "BROWSER_SMOKE_ARTIFACT_DIR";

export function resolveBrowserSmokeArtifactDir({ repoRoot, env = process.env, tempRoot = tmpdir() }) {
  const override = env[artifactDirVariable];
  if (!override) {
    const checkout = createHash("sha256").update(repoRoot).digest("hex").slice(0, 12);
    return join(tempRoot, "launchplane-browser-smoke", checkout);
  }
  // Relative values resolve against the checkout, so CI's value also works
  // inside the browser container, where the checkout is mounted elsewhere.
  const directory = resolve(repoRoot, override);
  const fromDirectory = relative(directory, repoRoot);
  // Each run deletes this directory first; never let that reach the checkout.
  const outside = fromDirectory === ".." || fromDirectory.startsWith(`..${sep}`) || isAbsolute(fromDirectory);
  if (!outside) {
    throw new Error(`${artifactDirVariable} must not be the checkout or contain it: ${directory}`);
  }
  return directory;
}

const repoRoot = realpathSync(fileURLToPath(new URL("../", import.meta.url)));

export const browserSmokeArtifactDir = resolveBrowserSmokeArtifactDir({ repoRoot });

export function smokeArtifact(...segments) {
  return join(browserSmokeArtifactDir, ...segments);
}
