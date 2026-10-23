import { rmSync } from "node:fs";
import { browserSmokeArtifactDir } from "../browser-smoke-paths.mjs";

rmSync(browserSmokeArtifactDir, { force: true, recursive: true });
