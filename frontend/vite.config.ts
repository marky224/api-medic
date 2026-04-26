import { defineConfig, type Plugin } from "vitest/config";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = path.resolve(here, "..", "tests", "fixtures", "reports");
const URL_PREFIX = "/fixtures/";

function listFixtureFiles(): string[] {
  return fs
    .readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith(".json"))
    .sort();
}

// Serves tests/fixtures/reports/*.json under /fixtures/ in dev and emits
// the same files (plus a manifest index.json) into the build output. Phase 2
// uses these as the data source; Phase 3 swaps them for /api/run + /api/analyze.
function fixturesPlugin(): Plugin {
  return {
    name: "api-medic-fixtures",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url ?? "";
        if (!url.startsWith(URL_PREFIX)) return next();
        const rel = url.slice(URL_PREFIX.length).split("?")[0] ?? "";
        if (rel === "index.json") {
          const body = JSON.stringify(
            listFixtureFiles().map((f) => ({
              id: f.replace(/\.json$/, ""),
              filename: f,
            })),
          );
          res.setHeader("Content-Type", "application/json");
          res.end(body);
          return;
        }
        if (!/^[\w.-]+\.json$/.test(rel)) return next();
        const fp = path.join(FIXTURES_DIR, rel);
        fs.readFile(fp, (err, buf) => {
          if (err) {
            res.statusCode = 404;
            res.end();
            return;
          }
          res.setHeader("Content-Type", "application/json");
          res.end(buf);
        });
      });
    },
    generateBundle() {
      const files = listFixtureFiles();
      for (const f of files) {
        this.emitFile({
          type: "asset",
          fileName: `fixtures/${f}`,
          source: fs.readFileSync(path.join(FIXTURES_DIR, f)),
        });
      }
      this.emitFile({
        type: "asset",
        fileName: "fixtures/index.json",
        source: JSON.stringify(
          files.map((f) => ({ id: f.replace(/\.json$/, ""), filename: f })),
        ),
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), fixturesPlugin()],
  server: {
    fs: {
      allow: [path.resolve(here, ".."), here],
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
