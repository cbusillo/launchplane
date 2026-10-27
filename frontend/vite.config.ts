import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";

export default defineConfig(({ command, mode }) => {
  let cacheDir: string | undefined;
  if (command === "serve" && mode === "browser-smoke") {
    // Outside the checkout, so a killed server leaves nothing in the worktree.
    cacheDir = mkdtempSync(join(tmpdir(), "launchplane-vite-cache-"));
  }
  function removeCache() {
    if (cacheDir) rmSync(cacheDir, { recursive: true, force: true });
  }
  if (cacheDir) process.once("exit", removeCache);
  return {
    base: "/ui/",
    cacheDir,
    plugins: [react()],
    build: {
      outDir: "../control_plane/ui_static",
      emptyOutDir: true
    },
    server: {
      proxy: {
        "/v1": "http://127.0.0.1:8080"
      }
    }
  };
});
