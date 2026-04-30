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
npm run build         # one-shot — produces dist/
npm run dev           # watch — rebuilds on source change
npm run package       # zips dist/ into packages/ for both Chrome and Firefox
```

`dist/` contains both `manifest.json` (Chrome) and `manifest.firefox.json`
(Firefox). For unpacked development load whichever manifest your browser
expects — see below.

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
