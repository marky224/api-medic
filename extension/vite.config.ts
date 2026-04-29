import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

// Multi-page build: devtools.html registers the panel, panel.html is the
// React UI. Output goes to extension/dist/, which is what the user loads
// as an unpacked extension. Manifest is copied from public/ by Vite.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@frontend": path.resolve(here, "..", "frontend", "src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        devtools: path.resolve(here, "devtools.html"),
        panel: path.resolve(here, "panel.html"),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    fs: {
      // Allow Vite to read shared components from ../frontend/src.
      allow: [path.resolve(here, ".."), here],
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
