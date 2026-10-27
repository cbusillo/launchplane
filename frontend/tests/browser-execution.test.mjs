import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { browserDeadlines, browserWorkers } from "../playwright-deadlines.mjs";

test("local and CI browser execution share finite resource limits", async () => {
  const scratch = await mkdtemp(join(tmpdir(), "launchplane-browser-limits-"));
  const original = fileURLToPath(new URL("../playwright.config.ts", import.meta.url));
  const cli = fileURLToPath(import.meta.resolve("@playwright/test/cli"));
  try {
    await writeFile(join(scratch, "package.json"), JSON.stringify({ type: "module" }));
    await writeFile(join(scratch, "playwright.config.ts"), `
import original from ${JSON.stringify(original)};
export default {
  ...original,
  testDir: ${JSON.stringify(scratch)},
  projects: [original.projects[0]],
};
`);
    await writeFile(join(scratch, "limits.spec.ts"), `
import { test } from ${JSON.stringify(fileURLToPath(import.meta.resolve("@playwright/test")))};
test("lists without starting a browser", () => {});
`);
    await writeFile(join(scratch, "reporter.mjs"), `
export default class {
  onBegin(config) {
    const project = config.projects[0];
    console.log(JSON.stringify({
      workers: config.workers,
      suite: config.globalTimeout,
      test: project.timeout,
      action: project.use.actionTimeout,
      navigation: project.use.navigationTimeout,
    }));
  }
}
`);
    for (const ci of [undefined, "true"]) {
      const env = { ...process.env };
      if (ci === undefined) delete env.CI;
      else env.CI = ci;
      const run = spawnSync(process.execPath, [cli, "test", "--list",
        `--config=${join(scratch, "playwright.config.ts")}`,
        `--reporter=${join(scratch, "reporter.mjs")}`], {
        env, encoding: "utf8", timeout: browserDeadlines.server,
      });
      assert.equal(run.status, 0, run.stderr || run.error?.message);
      const limits = JSON.parse(run.stdout.trim());
      assert.deepEqual(limits, {
        workers: browserWorkers,
        suite: browserDeadlines.suite,
        test: browserDeadlines.test,
        action: browserDeadlines.assertion,
        navigation: browserDeadlines.assertion,
      }, `CI=${ci ?? "unset"}`);
      for (const limit of Object.values(limits)) assert.ok(Number.isFinite(limit) && limit > 0);
    }
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
});
