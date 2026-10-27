import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative } from "node:path";
import { test } from "node:test";
import { resetBrowserSmokeArtifactDir, resolveBrowserSmokeArtifactDir } from "../browser-smoke-paths.mjs";

const tempRoot = "/scratch/os-temp";

function inside(parent, child) {
  const path = relative(parent, child);
  return path !== "" && !path.startsWith("..") && !isAbsolute(path);
}

test("local evidence goes to a per-checkout folder outside the checkout", () => {
  const first = resolveBrowserSmokeArtifactDir({ repoRoot: "/work/one", env: {}, tempRoot });
  const second = resolveBrowserSmokeArtifactDir({ repoRoot: "/work/two", env: {}, tempRoot });
  assert.ok(inside(tempRoot, first), first);
  assert.ok(!inside("/work/one", first), first);
  assert.notEqual(first, second);
  assert.equal(resolveBrowserSmokeArtifactDir({ repoRoot: "/work/one", env: {}, tempRoot }), first);
});

test("the override resolves relative paths against the checkout", () => {
  const env = { BROWSER_SMOKE_ARTIFACT_DIR: "tmp/browser-smoke" };
  assert.equal(resolveBrowserSmokeArtifactDir({ repoRoot: "/work/one", env, tempRoot }),
    join("/work/one", "tmp/browser-smoke"));
  assert.equal(resolveBrowserSmokeArtifactDir({ repoRoot: "/work/one", env: { BROWSER_SMOKE_ARTIFACT_DIR: "/evidence" }, tempRoot }),
    "/evidence");
});

test("reset clears only a folder a smoke run created", async () => {
  const scratch = await mkdtemp(join(tmpdir(), "launchplane-smoke-reset-"));
  try {
    const fresh = join(scratch, "fresh");
    resetBrowserSmokeArtifactDir(fresh);
    await writeFile(join(fresh, "old.png"), "old evidence");
    resetBrowserSmokeArtifactDir(fresh);
    assert.ok(!existsSync(join(fresh, "old.png")));

    const empty = join(scratch, "empty");
    await mkdir(empty);
    resetBrowserSmokeArtifactDir(empty);

    const shared = join(scratch, "shared");
    await mkdir(shared);
    await writeFile(join(shared, "notes.md"), "someone else's file");
    assert.throws(() => resetBrowserSmokeArtifactDir(shared));
    assert.deepEqual(await readdir(shared), ["notes.md"]);
  } finally {
    await rm(scratch, { force: true, recursive: true });
  }
});
