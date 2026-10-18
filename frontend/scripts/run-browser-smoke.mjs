import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const shard = process.env.BROWSER_SMOKE_SHARD;
const args = [fileURLToPath(import.meta.resolve("@playwright/test/cli")), "test"];
const env = { ...process.env };
if (shard !== undefined) {
  const match = /^([1-9]\d*)\/([1-9]\d*)$/.exec(shard);
  if (!match || !Number.isSafeInteger(Number(match[2])) || Number(match[1]) > Number(match[2])) {
    throw new Error("BROWSER_SMOKE_SHARD must be a valid Playwright shard index/count");
  }
  args.push(`--shard=${shard}`, "--reporter=line,json",
    `--output=../tmp/browser-smoke/test-results/shard-${match[1]}`);
  env.PLAYWRIGHT_JSON_OUTPUT_FILE = fileURLToPath(
    new URL(`../../tmp/browser-smoke/reports/shard-${match[1]}.json`, import.meta.url));
}
const run = spawnSync(process.execPath, args, {
  cwd: fileURLToPath(new URL("../", import.meta.url)), env, stdio: "inherit",
});
if (run.status !== 0) process.exit(run.status ?? 1);
// CI checks the combined evidence after every shard succeeds. The ordinary
// local command still requires the complete screenshot set immediately.
if (shard === undefined) await import("./assert-browser-smoke-artifacts.mjs");
