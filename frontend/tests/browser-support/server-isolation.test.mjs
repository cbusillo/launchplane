import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";
import { test } from "node:test";

const frontendRoot = fileURLToPath(new URL("../../", import.meta.url));
const cli = fileURLToPath(import.meta.resolve("@playwright/test/cli"));

test("overlapping smoke runs own distinct servers", { timeout: 90_000 }, async () => {
  const scratchRoot = resolve(frontendRoot, "../tmp/browser-isolation");
  await mkdir(scratchRoot, { recursive: true });
  const scratch = await mkdtemp(join(scratchRoot, "run-"));
  const children = [];
  try {
    await writeFile(join(scratch, "package.json"), JSON.stringify({ type: "module" }));
    const receipts = [join(scratch, "one.json"), join(scratch, "two.json")];
    /** @type {Array<{code: number | null, output: string}>} */
    const runs = await Promise.all(receipts.map(async (receipt, index) => {
      const root = join(scratch, String(index));
      await mkdir(root);
      const config = join(root, "playwright.config.ts");
      await writeFile(config, `
import original from ${JSON.stringify(join(frontendRoot, "playwright.config.ts"))};
export default {
  ...original,
  testDir: ${JSON.stringify(root)},
  outputDir: ${JSON.stringify(join(root, "results"))},
  workers: 1,
  projects: [original.projects[0]],
  webServer: { ...original.webServer, cwd: ${JSON.stringify(frontendRoot)}, timeout: 15_000 },
};
`);
      await writeFile(join(root, "isolation.spec.ts"), `
import { test, expect } from ${JSON.stringify(join(frontendRoot, "node_modules/@playwright/test/index.mjs"))};
import { writeFile, access } from "node:fs/promises";
test("serves this run while its peer is alive", async ({ page, baseURL }) => {
  test.setTimeout(45_000);
  await page.goto("/ui/?fixture=products");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await writeFile(${JSON.stringify(receipt)}, JSON.stringify({ baseURL }));
  await expect.poll(async () => {
    try { await access(${JSON.stringify(receipts[1 - index])}); return true; }
    catch { return false; }
  }, { timeout: 30_000 }).toBe(true);
});
`);
      return new Promise((resolveRun, rejectRun) => {
        const child = spawn(process.execPath, [cli, "test", `--config=${config}`], {
          cwd: frontendRoot,
          env: { ...process.env, BROWSER_SMOKE_URL: "" },
          stdio: ["ignore", "pipe", "pipe"],
        });
        children.push(child);
        let output = "";
        function appendOutput(data) { output += data; }
        child.stdout.on("data", appendOutput);
        child.stderr.on("data", appendOutput);
        child.on("error", rejectRun);
        child.on("close", code => resolveRun({ code, output }));
      });
    }));
    for (const run of runs) assert.equal(run.code, 0, run.output);
    const urls = await Promise.all(receipts.map(async receipt =>
      JSON.parse(await readFile(receipt, "utf8")).baseURL));
    assert.notEqual(urls[0], urls[1]);
  } finally {
    for (const child of children) if (child.exitCode === null) child.kill();
    await rm(scratch, { force: true, recursive: true });
  }
});
