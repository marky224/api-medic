import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

// Multi-page build: devtools.html registers the panel, panel.html is the
// React UI. Output goes to extension/dist/, which is what the user loads
// as an unpacked extension. Manifest is copied from public/ by Vite.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@frontend": path.resolve(here, "..", "frontend", "src"),
    },
    // Files under @frontend would otherwise resolve react from
    // ../frontend/node_modules and bundle a second copy (invalid hook
    // call). @vitejs/plugin-react set this itself before v6.
    dedupe: ["react", "react-dom"],
  },
  build: {
    // Match minimum_chrome_version / strict_min_version in the manifests;
    // Vite's default target is newer than both.
    target: ["chrome102", "firefox115"],
    outDir: "dist",
    emptyOutDir: true,
    rolldownOptions: {
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
