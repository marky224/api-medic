# api-medic browser extension

A DevTools panel that captures HTTP requests from the page being inspected
and posts them to the hosted api-medic analyzer at
`https://api-medic.markandrewmarquez.com/api/analyze`. The same `Report`
shape every other api-medic surface (CLI, web UI, hosted demo) returns is
rendered inline in the panel via the `ReportView` React component imported
directly from the main frontend.

## Develop

```sh
cd extension
npm install
npm run build           # one-shot — produces dist/
npm run dev             # watch — rebuilds on source change
npm run package         # zips dist/ into packages/ for both Chrome and Firefox
npm run package-source  # zips reviewable source for AMO (Mozilla) submission
```

`dist/` is gitignored — run `npm install && npm run build` first to
populate it. The build emits both `manifest.json` (Chrome) and
`manifest.firefox.json` (Firefox); for unpacked development load
whichever manifest your browser expects — see below.

### Load on Chrome / Edge / Brave / other Chromium

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/dist` directory

### Load on Firefox

Unpacked Firefox loading reads `manifest.json`, which is the Chrome variant
in `dist/`. Two options:

- **Quick test:** in `dist/`, rename `manifest.json` → `manifest.chrome.json`
  and `manifest.firefox.json` → `manifest.json`, then load.
- **Submission-ready:** run `npm run package` and load
  `packages/api-medic-firefox-X.Y.Z.zip` via `about:debugging` →
  **This Firefox** → **Load Temporary Add-on…** (selecting the zip works on
  Firefox 109+; older versions need an unzipped folder).

Open DevTools on any page → look for the **api-medic** panel beside
**Network**, **Console**, etc. Requests made while DevTools is open are
captured into the panel; pick one and click **Analyze with api-medic**.

## Build target

Manifest v3. Chrome ≥ 102 and Firefox ≥ 115. The same source compiles for
both because Firefox MV3 aliases the `chrome.*` namespace to `browser.*`,
so the panel's `chrome.devtools.network.onRequestFinished` listener works
unchanged. Submission packaging differs — see `scripts/package.mjs`, which
emits one zip per store with the correct manifest under the canonical
`manifest.json` name.

## Source submission (AMO)

AMO requires a separate source zip when the published add-on is bundled or
minified (Vite minifies in production). `npm run package-source` produces
`packages/api-medic-source-X.Y.Z.zip` containing a top-level `BUILD.md`,
`LICENSE`, the `extension/` source tree, and the slice of `frontend/` the
extension imports via the `@frontend` Vite alias. A reviewer following
`BUILD.md` ends up with a Firefox zip whose `manifest.json` matches the
submitted add-on byte-for-byte; CI runs that rebuild on every push.

## Architecture

The extension is a thin capture layer; analysis runs server-side on the
existing Lambda. The panel only:

1. Listens for `chrome.devtools.network.onRequestFinished`. Chrome
   delivers entries that already match the HAR `entries[i]` shape.
2. Wraps the user's selected entry in a single-entry HAR log via
   `lib/serialize.ts`.
3. POSTs to `/api/analyze`. The manifest's `host_permissions` declaration
   for the analyzer host lets the DevTools panel's `fetch()` skip the
   CORS preflight, so no API Gateway change is needed for the extension.
4. Renders the returned `Report` with `ReportView` (shared with the web
   UI via the Vite alias `@frontend` → `../frontend/src`).

`Report` and the rest of the type surface come from
`frontend/src/lib/types.ts`, which is auto-generated from the Pydantic
models by `make types` at the repo root. Run that whenever the schema
changes.

## Limitations

- The panel only sees requests the browser actually completes.
  Connections the browser refuses at its own layer — expired or
  untrusted certs, mixed-content blocks, CSP-blocked subresources —
  never fire `chrome.devtools.network.onRequestFinished`, so they
  don't appear in the panel and can't be analyzed. For diagnosing
  those, capture a curl reproduction or paste a HAR into the hosted
  demo at `https://api-medic.markandrewmarquez.com`.
- Captures are scoped to the page DevTools is attached to. Background
  tabs, service-worker requests, and requests issued before DevTools
  was opened won't appear.
- All analysis happens server-side on the hosted Lambda. Without
  network access to `api-medic.markandrewmarquez.com` the panel
  can capture but not analyze.
