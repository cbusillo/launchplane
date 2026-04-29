import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/ui/",
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
});
