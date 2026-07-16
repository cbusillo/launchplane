import { rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const artifactDir = fileURLToPath(
  new URL("../../tmp/browser-smoke/", import.meta.url),
);

rmSync(artifactDir, { force: true, recursive: true });
