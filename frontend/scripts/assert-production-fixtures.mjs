import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const assetDir = new URL("../../control_plane/ui_static/assets/", import.meta.url);
const javascriptAssets = readdirSync(assetDir).filter((name) => name.endsWith(".js"));
const forbiddenNeedles = ["development fixtures", "fixture evidence", "Operator state coverage"];

for (const assetName of javascriptAssets) {
  const assetText = readFileSync(join(assetDir.pathname, assetName), "utf8");
  for (const needle of forbiddenNeedles) {
    if (assetText.includes(needle)) {
      throw new Error(`Production UI asset ${assetName} contains dev fixture text: ${needle}`);
    }
  }
}
