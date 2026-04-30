#!/usr/bin/env node
// Validates that the extension build produced loadable Manifest v3 packages
// for both Chrome and Firefox. Catches Vite/Rollup misconfiguration and
// manifest drift before artifacts are shipped to the Web Store / AMO.
// Runs after `vite build` (chained from the build script).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const dist = path.join(root, "dist");

const REQUIRED_FILES = [
  "manifest.json",
  "manifest.firefox.json",
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

const pkg = JSON.parse(
  fs.readFileSync(path.join(root, "package.json"), "utf8"),
);

function loadManifest(filename) {
  const p = path.join(dist, filename);
  if (!fs.existsSync(p)) return null;
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (e) {
    errors.push(`dist/${filename} is not valid JSON: ${e.message}`);
    return null;
  }
}

function checkCommon(manifest, label) {
  if (manifest.manifest_version !== 3) {
    errors.push(
      `${label}: manifest_version must be 3 (got ${JSON.stringify(manifest.manifest_version)})`,
    );
  }
  if (typeof manifest.devtools_page !== "string" || !manifest.devtools_page) {
    errors.push(`${label}: devtools_page must be a non-empty string`);
  }
  if (
    !Array.isArray(manifest.host_permissions) ||
    manifest.host_permissions.length === 0
  ) {
    errors.push(`${label}: host_permissions must be a non-empty array`);
  }
  if (manifest.version !== pkg.version) {
    errors.push(
      `${label} version (${manifest.version}) does not match package.json version (${pkg.version})`,
    );
  }
}

const chrome = loadManifest("manifest.json");
if (chrome) checkCommon(chrome, "dist/manifest.json (Chrome)");

const firefox = loadManifest("manifest.firefox.json");
if (firefox) {
  checkCommon(firefox, "dist/manifest.firefox.json (Firefox)");
  const gecko = firefox.browser_specific_settings?.gecko;
  if (!gecko || typeof gecko.id !== "string" || !gecko.id) {
    errors.push(
      "dist/manifest.firefox.json: browser_specific_settings.gecko.id is required for AMO submission",
    );
  } else if (
    !/^[^@\s]+@[^@\s]+$/.test(gecko.id) &&
    !/^\{[0-9a-fA-F-]{36}\}$/.test(gecko.id)
  ) {
    errors.push(
      `dist/manifest.firefox.json: gecko.id must be email-style (foo@bar) or UUID-in-braces, got "${gecko.id}"`,
    );
  }
  // AMO requires data_collection_permissions on all new submissions; without
  // it the validator hard-fails before a human reviewer ever sees the listing.
  const dcp = gecko?.data_collection_permissions;
  if (!dcp || !Array.isArray(dcp.required) || dcp.required.length === 0) {
    errors.push(
      "dist/manifest.firefox.json: browser_specific_settings.gecko.data_collection_permissions.required must be a non-empty array (use [\"none\"] if nothing is collected)",
    );
  }
}

if (chrome && firefox && chrome.version !== firefox.version) {
  errors.push(
    `Chrome and Firefox manifest versions disagree: ${chrome.version} vs ${firefox.version}`,
  );
}

if (errors.length > 0) {
  console.error("verify-dist: failed");
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}

console.log("verify-dist: OK (Chrome + Firefox manifests)");
