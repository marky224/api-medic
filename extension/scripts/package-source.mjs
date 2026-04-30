#!/usr/bin/env node
// Produces api-medic-source-X.Y.Z.zip for AMO's source-code submission
// requirement (Mozilla rejects builds with minified/bundled output unless
// reviewers can reproduce the build from a separate source zip).
//
// Layout:
//   BUILD.md                generated each run; reviewer's instructions
//   LICENSE                 from repo root
//   extension/...           extension source minus build artefacts and deps
//   frontend/src/...        slice the @frontend Vite alias resolves to
//
// The reviewer follows BUILD.md (cd extension && npm ci && npm run build &&
// npm run package) and ends up with the same Firefox zip we submitted.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import archiver from "archiver";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const repoRoot = path.resolve(root, "..");
const out = path.join(root, "packages");

const pkg = JSON.parse(
  fs.readFileSync(path.join(root, "package.json"), "utf8"),
);
const version = pkg.version;

fs.mkdirSync(out, { recursive: true });

// Names at extension/'s top level that should NOT go in the source zip.
const EXCLUDE = new Set([
  "node_modules",
  "dist",
  "packages",
  ".vite",
  "tsconfig.tsbuildinfo",
  "tsconfig.node.tsbuildinfo",
  // Emitted by `tsc -b` from vite.config.ts; the .ts source ships, the
  // emitted .js/.d.ts would only confuse reviewers.
  "vite.config.js",
  "vite.config.d.ts",
  "vite.config.d.ts.map",
]);

const buildMd = `# Build instructions for AMO review

This source corresponds to api-medic Firefox add-on version ${version}.

## Requirements

- Node.js 20.x
- npm 10.x or later

## Build

\`\`\`sh
# install deps in frontend/ first — the extension type-checks files
# imported via the @frontend Vite alias, and TypeScript walks up from
# those files to resolve their react / react-dom imports
cd frontend
npm ci

cd ../extension
npm ci
npm run build
npm run package
\`\`\`

## Expected output

\`extension/packages/api-medic-firefox-${version}.zip\` matches the submitted
Firefox add-on. (\`api-medic-chrome-${version}.zip\` is also produced for
the parallel Chrome Web Store submission.)

## Layout of this archive

- \`extension/\` — Manifest v3 DevTools panel source, build configuration, scripts
- \`frontend/\` — shared React components imported by the extension via
  the \`@frontend\` Vite alias declared in \`extension/vite.config.ts\`. The
  panel renders a \`ReportView\` component shared with the project's web UI
  so the rendered diagnostic output is identical across surfaces.
  \`frontend/package.json\` and \`frontend/package-lock.json\` are included so
  \`npm ci\` resolves the React types TypeScript needs while type-checking
  those imported files; only \`frontend/src/\` is otherwise relevant to the
  extension build.

## Architecture

The extension is a Manifest v3 DevTools panel. It captures HTTP requests via
\`chrome.devtools.network.onRequestFinished\` (Firefox MV3 aliases this from
the \`browser.*\` namespace, so the same source compiles for both browsers),
wraps the user-selected request in a single-entry HAR payload, and POSTs it
to https://api-medic.markandrewmarquez.com/api/analyze for diagnosis. The
returned diagnostic Report is rendered inline in the panel.

## Source repository

https://github.com/marky224/api-medic — MIT licensed (see LICENSE).
`;

async function main() {
  const zipPath = path.join(out, `api-medic-source-${version}.zip`);
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

  archive.append(buildMd, { name: "BUILD.md" });

  const licensePath = path.join(repoRoot, "LICENSE");
  if (!fs.existsSync(licensePath)) {
    throw new Error(`LICENSE not found at ${licensePath}`);
  }
  archive.file(licensePath, { name: "LICENSE" });

  for (const name of fs.readdirSync(root)) {
    if (EXCLUDE.has(name)) continue;
    const full = path.join(root, name);
    const stat = fs.statSync(full);
    if (stat.isDirectory()) archive.directory(full, `extension/${name}`);
    else archive.file(full, { name: `extension/${name}` });
  }

  const frontendDir = path.join(repoRoot, "frontend");
  for (const name of ["package.json", "package-lock.json"]) {
    const p = path.join(frontendDir, name);
    if (!fs.existsSync(p)) {
      throw new Error(`frontend/${name} not found at ${p}`);
    }
    archive.file(p, { name: `frontend/${name}` });
  }
  const frontendSrc = path.join(frontendDir, "src");
  if (!fs.existsSync(frontendSrc)) {
    throw new Error(`frontend/src not found at ${frontendSrc}`);
  }
  archive.directory(frontendSrc, "frontend/src");

  await archive.finalize();
  await finished;

  const size = fs.statSync(zipPath).size;
  console.log(
    `  ${path.relative(root, zipPath)}  (${(size / 1024).toFixed(1)} KB)`,
  );
}

console.log(`package-source: building source zip for v${version}`);
try {
  await main();
  console.log("package-source: OK");
} catch (e) {
  console.error(`package-source: failed — ${e.message}`);
  process.exit(1);
}
