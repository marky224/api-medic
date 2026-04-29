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
npm run build         # one-shot
npm run dev           # watch — rebuilds on source change
```

Then load `extension/dist/` as an unpacked extension:

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/dist` directory

Open DevTools on any page → look for the **api-medic** panel beside
**Network**, **Console**, etc. Requests made while DevTools is open are
captured into the panel; pick one and click **Analyze with api-medic**.

## Build target

Manifest v3, Chrome ≥ 102. Firefox add-on packaging is handled in a
follow-up — the same source builds on both because we use only the
standard `chrome.devtools.*` API surface (which Firefox implements via
its compatibility shim).

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
