import assert from "node:assert/strict";
import { isAbsolute, join, relative } from "node:path";
import { test } from "node:test";
import { resolveBrowserSmokeArtifactDir } from "../browser-smoke-paths.mjs";

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

test("the override never names the checkout or a folder holding it", () => {
  for (const value of [".", "/work/one", "/work", "/", "frontend/../"]) {
    assert.throws(() => resolveBrowserSmokeArtifactDir({
      repoRoot: "/work/one", env: { BROWSER_SMOKE_ARTIFACT_DIR: value }, tempRoot,
    }), /must not be the checkout/, value);
  }
});
