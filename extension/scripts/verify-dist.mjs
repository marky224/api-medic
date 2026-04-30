#!/usr/bin/env node
// Validates that the extension build produced a loadable Manifest v3 package.
// Catches Vite/Rollup misconfiguration before the artifact is shipped to the
// Chrome Web Store. Runs after `vite build` (chained from the build script).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const dist = path.join(root, "dist");

const REQUIRED_FILES = [
  "manifest.json",
  "devtools.html",
  "panel.html",
  "devtools.js",
  "panel.js",
];

const errors = [];

if (!fs.existsSync(dist)) {
  console.error(`verify-dist: dist directory not found at ${dist}`);
  console.error("verify-dist: run 'npm run build' first.");
  process.exit(1);
}

for (const f of REQUIRED_FILES) {
  if (!fs.existsSync(path.join(dist, f))) {
    errors.push(`missing required file: dist/${f}`);
  }
}

const manifestPath = path.join(dist, "manifest.json");
if (fs.existsSync(manifestPath)) {
  let manifest = null;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (e) {
    errors.push(`dist/manifest.json is not valid JSON: ${e.message}`);
  }

  if (manifest) {
    if (manifest.manifest_version !== 3) {
      errors.push(
        `dist/manifest.json: manifest_version must be 3 (got ${JSON.stringify(manifest.manifest_version)})`,
      );
    }
    if (typeof manifest.devtools_page !== "string" || !manifest.devtools_page) {
      errors.push("dist/manifest.json: devtools_page must be a non-empty string");
    }
    if (
      !Array.isArray(manifest.host_permissions) ||
      manifest.host_permissions.length === 0
    ) {
      errors.push(
        "dist/manifest.json: host_permissions must be a non-empty array",
      );
    }

    const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
    if (manifest.version !== pkg.version) {
      errors.push(
        `dist/manifest.json version (${manifest.version}) does not match package.json version (${pkg.version})`,
      );
    }
  }
}

if (errors.length > 0) {
  console.error("verify-dist: failed");
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}

console.log("verify-dist: OK");
