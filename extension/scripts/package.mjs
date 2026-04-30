#!/usr/bin/env node
// Produces submission-ready zips from dist/:
//   - api-medic-chrome-X.Y.Z.zip   (manifest.json from public/manifest.json)
//   - api-medic-firefox-X.Y.Z.zip  (manifest.json sourced from manifest.firefox.json)
// Each zip contains a single manifest.json — Chrome ignores firefox-specific
// keys but bundling both manifests would still confuse the Web Store reviewer.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import archiver from "archiver";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const dist = path.join(root, "dist");
const out = path.join(root, "packages");

if (!fs.existsSync(dist)) {
  console.error("package: dist/ not found. Run 'npm run build' first.");
  process.exit(1);
}

const pkg = JSON.parse(
  fs.readFileSync(path.join(root, "package.json"), "utf8"),
);
const version = pkg.version;

fs.mkdirSync(out, { recursive: true });

// Files to include verbatim in both zips. We deliberately skip both manifest
// files at the top level — the right one is added under the canonical name
// `manifest.json` per browser.
const SKIP_AT_ROOT = new Set(["manifest.json", "manifest.firefox.json"]);

function entriesToInclude() {
  const entries = [];
  for (const name of fs.readdirSync(dist)) {
    if (SKIP_AT_ROOT.has(name)) continue;
    entries.push(name);
  }
  return entries;
}

async function buildZip(target, manifestSource) {
  const zipPath = path.join(out, `api-medic-${target}-${version}.zip`);
  if (fs.existsSync(zipPath)) fs.unlinkSync(zipPath);

  const output = fs.createWriteStream(zipPath);
  const archive = archiver("zip", { zlib: { level: 9 } });

  const finished = new Promise((resolve, reject) => {
    output.on("close", resolve);
    archive.on("warning", (err) => {
      if (err.code === "ENOENT") console.warn(`archive warning: ${err.message}`);
      else reject(err);
    });
    archive.on("error", reject);
  });

  archive.pipe(output);

  for (const name of entriesToInclude()) {
    const full = path.join(dist, name);
    const stat = fs.statSync(full);
    if (stat.isDirectory()) archive.directory(full, name);
    else archive.file(full, { name });
  }

  // Inject the manifest under the canonical name.
  const manifestPath = path.join(dist, manifestSource);
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`Manifest source not found: ${manifestPath}`);
  }
  archive.file(manifestPath, { name: "manifest.json" });

  await archive.finalize();
  await finished;

  const size = fs.statSync(zipPath).size;
  console.log(`  ${path.relative(root, zipPath)}  (${(size / 1024).toFixed(1)} KB)`);
  return zipPath;
}

console.log(`package: building zips for v${version}`);
await buildZip("chrome", "manifest.json");
await buildZip("firefox", "manifest.firefox.json");
console.log("package: OK");
