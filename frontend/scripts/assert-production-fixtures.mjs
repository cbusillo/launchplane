import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const assetDir = fileURLToPath(
  new URL("../../control_plane/ui_static/assets/", import.meta.url),
);
const javascriptAssets = readdirSync(assetDir).filter((name) => name.endsWith(".js"));
const forbiddenNeedles = [
  "development fixtures",
  "fixture evidence",
  "Operator state coverage",
  "Atlas Commerce",
  "Demo Operator",
  "fixture product inventory is intentionally unavailable",
  "Launchplane engineering fixture evidence",
  "engineering fixture inventory is intentionally unavailable",
  "engineering fixture refresh is intentionally unavailable",
  "fixture-engineering-denied",
];

for (const assetName of javascriptAssets) {
  const assetText = readFileSync(join(assetDir, assetName), "utf8");
  for (const needle of forbiddenNeedles) {
    if (assetText.includes(needle)) {
      throw new Error(`Production UI asset ${assetName} contains dev fixture text: ${needle}`);
    }
  }
}
