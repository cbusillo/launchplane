import { statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const screenshotDir = fileURLToPath(
  new URL("../../tmp/browser-smoke/screenshots/", import.meta.url),
);
const projects = ["desktop", "narrow"];
const journeys = [
  "anonymous-auth-prompt",
  "product-inventory-empty",
  "product-inventory-error",
  "product-workspace",
  "product-activity",
  "environment-diagnostics",
  "blocked-action",
  "safe-change-confirmation",
];
const missingArtifacts = [];

for (const project of projects) {
  for (const journey of journeys) {
    const screenshotPath = join(screenshotDir, project, `${journey}.png`);
    try {
      const screenshot = statSync(screenshotPath);
      if (!screenshot.isFile() || screenshot.size === 0) {
        missingArtifacts.push(screenshotPath);
      }
    } catch {
      missingArtifacts.push(screenshotPath);
    }
  }
}

if (missingArtifacts.length) {
  throw new Error(
    `Browser smoke did not produce fresh screenshot evidence:\n${missingArtifacts.join("\n")}`,
  );
}

console.log(
  `Verified ${projects.length * journeys.length} browser smoke screenshots.`,
);
