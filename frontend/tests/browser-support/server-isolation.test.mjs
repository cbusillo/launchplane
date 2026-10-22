import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";
import { test } from "node:test";
import { expect } from "@playwright/test";
import { browserDeadlines, isolationDeadline } from "../../playwright-deadlines.mjs";

const frontendRoot = fileURLToPath(new URL("../../", import.meta.url));
const cli = fileURLToPath(import.meta.resolve("@playwright/test/cli"));

async function cacheExists(path) {
  try { await stat(path); return true; }
  catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

function runPlaywright(config, children) {
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
}

test("overlapping smoke runs own distinct servers", { timeout: 2 * isolationDeadline }, async () => {
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
import { isolationDeadline } from ${JSON.stringify(join(frontendRoot, "playwright-deadlines.mjs"))};
export default {
  ...original,
  testDir: ${JSON.stringify(root)},
  outputDir: ${JSON.stringify(join(root, "results"))},
  workers: 1,
  timeout: isolationDeadline,
  expect: { ...original.expect, timeout: isolationDeadline },
  projects: [original.projects[0]],
  webServer: { ...original.webServer, cwd: ${JSON.stringify(frontendRoot)} },
};
`);
      await writeFile(join(root, "isolation.spec.ts"), `
import { test, expect, waitForState } from ${JSON.stringify(join(frontendRoot, "tests/browser/state.ts"))};
import { existsSync } from "node:fs";
import { writeFile, access, readFile, realpath, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
test("serves this run while its peer is alive", async ({ page, baseURL }) => {
  const dependencyModules: string[] = [];
  page.on("response", response => {
    const url = new URL(response.url());
    if (url.pathname.includes("/deps/") && url.pathname.endsWith(".js")) {
      dependencyModules.push(url.href);
    }
  });
  const entryURL = new URL("/ui/?fixture=products", baseURL);
  await page.goto(entryURL.href);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(dependencyModules.length).toBeGreaterThan(0);
  const pathname = decodeURIComponent(new URL(dependencyModules[0]).pathname);
  const absolutePrefix = pathname.indexOf("/@fs/");
  const backingPath = absolutePrefix >= 0
    ? pathname.slice(absolutePrefix + "/@fs".length)
    : resolve(${JSON.stringify(frontendRoot)}, pathname.slice(entryURL.pathname.length));
  expect((await stat(backingPath)).isFile()).toBe(true);
  const cacheDirectory = dirname(dirname(await realpath(backingPath)));
  await writeFile(${JSON.stringify(receipt)}, JSON.stringify({ baseURL, cacheDirectory }));
  await waitForState(async () => {
    try { await access(${JSON.stringify(receipts[1 - index])}); return true; }
    catch { return false; }
  }).toBe(true);
  const peer = JSON.parse(await readFile(${JSON.stringify(receipts[1 - index])}, "utf8"));
  expect(cacheDirectory).not.toBe(peer.cacheDirectory);
  if (${index} === 1) {
    await waitForState(() => existsSync(peer.cacheDirectory)).toBe(false);
    expect((await stat(cacheDirectory)).isDirectory()).toBe(true);
    await page.reload();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
});
`);
      return runPlaywright(config, children);
    }));
    for (const run of runs) assert.equal(run.code, 0, run.output);
    const facts = await Promise.all(receipts.map(async receipt =>
      JSON.parse(await readFile(receipt, "utf8"))));
    assert.notEqual(facts[0].baseURL, facts[1].baseURL);
    for (const fact of facts) {
      await expect.poll(() => cacheExists(fact.cacheDirectory), {
        timeout: browserDeadlines.assertion,
      }).toBe(false);
    }
  } finally {
    for (const child of children) if (child.exitCode === null) child.kill();
    await rm(scratch, { force: true, recursive: true });
  }
});

test("failed smoke startup removes its allocated cache", { timeout: isolationDeadline }, async () => {
  const scratchRoot = resolve(frontendRoot, "../tmp/browser-isolation");
  await mkdir(scratchRoot, { recursive: true });
  const scratch = await mkdtemp(join(scratchRoot, "startup-"));
  const children = [];
  try {
    await writeFile(join(scratch, "package.json"), JSON.stringify({ type: "module" }));
    const receipt = join(scratch, "cache.json");
    const viteConfig = join(scratch, "vite.config.ts");
    await writeFile(viteConfig, `
import original from ${JSON.stringify(join(frontendRoot, "vite.config.ts"))};
import { existsSync, writeFileSync } from "node:fs";
export default async environment => {
  const config = await original(environment);
  return {
    ...config,
    plugins: [...config.plugins, {
      name: "controlled-startup-failure",
      configureServer(server) {
        writeFileSync(${JSON.stringify(receipt)}, JSON.stringify({
          cacheDirectory: server.config.cacheDir,
          allocated: existsSync(server.config.cacheDir),
        }));
        throw new Error("Controlled startup failure");
      },
    }],
  };
};
`);
    const quotedConfig = "'" + viteConfig.replaceAll("'", "'\"'\"'") + "'";
    const config = join(scratch, "playwright.config.ts");
    await writeFile(config, `
import original from ${JSON.stringify(join(frontendRoot, "playwright.config.ts"))};
export default {
  ...original,
  testDir: ${JSON.stringify(scratch)},
  outputDir: ${JSON.stringify(join(scratch, "results"))},
  projects: [original.projects[0]],
  workers: 1,
  webServer: {
    ...original.webServer,
    cwd: ${JSON.stringify(frontendRoot)},
    command: original.webServer.command + ${JSON.stringify(" --config " + quotedConfig)},
  },
};
`);
    await writeFile(join(scratch, "startup.spec.ts"), `
import { test, expect } from ${JSON.stringify(join(frontendRoot, "tests/browser/state.ts"))};
test("renders if the server starts", async ({ page }) => {
  await page.goto("/ui/?fixture=products");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
});
`);
    const run = await runPlaywright(config, children);
    assert.notEqual(run.code, 0, run.output);
    const observed = JSON.parse(await readFile(receipt, "utf8"));
    assert.equal(observed.allocated, true);
    await expect.poll(() => cacheExists(observed.cacheDirectory), {
      timeout: browserDeadlines.assertion,
    }).toBe(false);
  } finally {
    for (const child of children) if (child.exitCode === null) child.kill();
    await rm(scratch, { force: true, recursive: true });
  }
});
