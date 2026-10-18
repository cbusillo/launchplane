import { readdir, readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

function testsIn(report) {
  if (report.errors?.length) throw new Error("Browser report contains errors");
  const tests = new Map();
  function visit(suites) {
    for (const suite of suites) {
      for (const spec of suite.specs ?? []) {
        for (const test of spec.tests) {
          const key = `${spec.id}:${test.projectId}`;
          if (!spec.id || !test.projectId || tests.has(key)) {
            throw new Error(`Invalid or duplicate browser test: ${key}`);
          }
          tests.set(key, { ...test, file: spec.file, title: spec.title });
        }
      }
      visit(suite.suites ?? []);
    }
  }
  visit(report.suites);
  return tests;
}

export function checkBrowserShardCoverage(inventory, reports) {
  const expected = testsIn(inventory);
  if (!expected.size) throw new Error("Browser inventory is empty");
  const seen = new Set();
  const modules = new Map();
  for (const [index, report] of reports.entries()) {
    if (report.config.shard?.current !== index + 1 || report.config.shard?.total !== reports.length) {
      throw new Error(`Browser shard ${index + 1} has the wrong identity`);
    }
    const tests = testsIn(report);
    if (!tests.size) throw new Error(`Browser shard ${index + 1} is empty`);
    for (const [key, test] of tests) {
      if (!expected.has(key) || seen.has(key)) {
        throw new Error(`Unexpected or repeated browser test: ${key}`);
      }
      if (expected.get(key).expectedStatus !== "passed" || test.expectedStatus !== "passed" ||
          test.results.length !== 1 || test.results[0].status !== "passed" || test.results[0].retry !== 0) {
        throw new Error(`Browser test did not pass once without retries: ${test.file}: ${test.title}`);
      }
      seen.add(key);
      modules.set(test.file, (modules.get(test.file) ?? 0) + test.results[0].duration);
    }
  }
  const missing = [...expected.keys()].filter(key => !seen.has(key));
  if (missing.length) throw new Error(`Missing browser tests: ${missing.join(", ")}`);
  return { tests: seen.size, modules };
}

async function main() {
  const frontendRoot = fileURLToPath(new URL("../", import.meta.url));
  const directory = resolve(frontendRoot, "../tmp/browser-smoke/reports");
  const filenames = (await readdir(directory)).sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  if (!filenames.length || filenames.some((name, index) => name !== `shard-${index + 1}.json`)) {
    throw new Error("Browser reports do not match the required shards");
  }
  // Discover the complete current suite independently of the shard reports.
  const cli = fileURLToPath(import.meta.resolve("@playwright/test/cli"));
  const result = spawnSync(process.execPath, [cli, "test", "--list", "--reporter=json"], {
    cwd: frontendRoot, encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(`Browser discovery failed: ${result.stderr}`);
  const reports = await Promise.all(filenames.map(async name =>
    JSON.parse(await readFile(resolve(directory, name), "utf8"))));
  const summary = checkBrowserShardCoverage(JSON.parse(result.stdout), reports);
  console.log(`Verified all ${summary.tests} browser tests passed once across ${reports.length} shards.`);
  for (const [file, duration] of [...summary.modules].sort((a, b) => b[1] - a[1]).slice(0, 10)) {
    console.log(`${file}: ${(duration / 1000).toFixed(1)} test-seconds`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
