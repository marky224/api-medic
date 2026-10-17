# api-medic browser extension

A DevTools panel that captures HTTP requests from the page being inspected
and posts them to the hosted api-medic analyzer at
`https://api-medic.markandrewmarquez.com/api/analyze`. The same `Report`
shape every other api-medic surface (CLI, web UI, hosted demo) returns is
rendered inline in the panel via the `ReportView` React component imported
directly from the main frontend.

## Install

- Chrome / Edge / other Chromium: [Chrome Web Store](https://chromewebstore.google.com/detail/api-medic/fhopbgedlmhdfbmnljihdfiijnmdjdoe)
- Firefox: [Firefox Add-ons](https://addons.mozilla.org/en-US/firefox/addon/api-medic/)

To build and load it from source instead, see below.

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
`BUILD.md` ends up with a Firefox zip whose files all match the submitted
add-on byte-for-byte; CI runs that rebuild on every push and diffs every
file.

## Architecture

The extension is a thin capture layer; analysis runs server-side on the
existing Lambda. The panel only:

1. Listens for `chrome.devtools.network.onRequestFinished`. Chrome
   delivers entries that already match the HAR `entries[i]` shape.
2. Masks credentials in a copy of the user's selected entry
   (`lib/redact.ts`; what is masked is listed in `PRIVACY.md`) and wraps it
   in a single-entry HAR log via `lib/serialize.ts`. The masking keeps what
   the checks read, and `tests/unit/test_redaction_parity.py` proves the
   findings match on masked and unmasked fixtures.
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
  untrusted certs (e.g. `https://expired.badssl.com`), mixed-content
  blocks, CSP-blocked subresources — never fire
  `chrome.devtools.network.onRequestFinished`, so they don't appear in
  the panel and can't be analyzed. For diagnosing those, capture a
  curl reproduction (`curl -v https://expired.badssl.com`) and run
  `api-medic from-curl` locally, or paste the failing request into
  the hosted demo at `https://api-medic.markandrewmarquez.com`.
- Captures are scoped to the page DevTools is attached to. Background
  tabs, service-worker requests, and requests issued before DevTools
  was opened won't appear.
- Masking works by name. A secret under a name the extension doesn't
  recognize, inside a URL path, in a body that isn't valid JSON or a
  URL-encoded form, or stored as a number is sent as captured.
- All analysis happens server-side on the hosted Lambda. Without
  network access to `api-medic.markandrewmarquez.com` the panel
  can capture but not analyze.
- The **api-medic** tab is missing on some pages. Two causes, neither
  fixable in the extension:
  - Chrome blocks every extension's `devtools_page` from loading on
    `chrome://*`, `chrome-extension://*`, `chrome-untrusted://*`, and
    the Chrome Web Store. Same restriction hits React DevTools, Redux
    DevTools, etc. See the
    [Chromium Extensions Security FAQ](https://chromium.googlesource.com/chromium/src/+/main/extensions/docs/security_faq.md).
  - Upstream Chrome bug since v99: devtools extensions occasionally
    fail to load on regular sites depending on whether DevTools opened
    before or after the page finished loading. Workaround: open
    DevTools first, then reload the page.
