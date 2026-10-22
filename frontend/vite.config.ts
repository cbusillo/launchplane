import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export default defineConfig(({ command, mode }) => {
  let cacheDir: string | undefined;
  if (command === "serve" && mode === "browser-smoke") {
    const cacheRoot = fileURLToPath(new URL("../tmp/browser-vite-cache/", import.meta.url));
    mkdirSync(cacheRoot, { recursive: true });
    cacheDir = mkdtempSync(join(cacheRoot, "server-"));
  }
  function removeCache() {
    if (cacheDir) rmSync(cacheDir, { recursive: true, force: true });
  }
  if (cacheDir) process.once("exit", removeCache);
  return {
    base: "/ui/",
    cacheDir,
    plugins: [react(), ...(cacheDir ? [{
      name: "browser-smoke-cache",
      closeServer() {
        removeCache();
        process.off("exit", removeCache);
      },
    }] : [])],
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
