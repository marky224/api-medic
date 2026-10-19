# Changelog

All notable changes to api-medic are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `README.md` and `extension/README.md` link the extension's Chrome Web Store and Firefox Add-ons listings.

### Changed

- The hosted demo deploy now waits for CI. `deploy-demo.yml` runs on `workflow_run` after the CI workflow completes and deploys only when that run succeeded for a push to `main`, checking out the exact commit CI tested. It skips the deploy if that commit is no longer `main`'s tip, so re-running an older commit's CI, or two pushes whose CI finishes out of order, can't deploy an older commit over a newer one. Deploys run one at a time. It never deploys for pull_request CI runs. Previously it ran on every push to `main` in parallel with CI, so a commit with red CI still deployed. Manual dispatch and the `DEMO_DEPLOY_ENABLED` gate are unchanged.
- Dependabot opens fewer PRs. Security updates are grouped into one PR per ecosystem directory, GitHub Actions updates (majors included) arrive as one PR, and npm semver-major version updates are ignored: majors are done as planned migrations that move the build tooling together, not as Dependabot PRs. Security updates that need a major bump still come through.
- The web UI and browser extension now build with Vite 8 (`@vitejs/plugin-react` 6), Tailwind CSS 4 (through `@tailwindcss/vite`, with the theme in each app's `src/index.css`; `tailwind.config.js` and `postcss.config.js` are gone) and test with Vitest 5 and jsdom 30. CI, the demo deploy and the PyPI release build them on Node 24: Vitest 5 needs Node 22.12+ and jsdom 30 needs Node 22.22.2+ or 24.15+. The UI looks the same in current desktop browsers: Tailwind 3's palette values and two of its Preflight defaults (the placeholder colour and the pointer cursor on buttons) are kept. Tailwind 4's default border colour (`currentColor`) is not changed back to Tailwind 3's gray, so a border needs a colour class; every border has one today. The web UI now builds for Vite 8's default targets (Chrome 111, Edge 111, Firefox 114, Safari 16.4): in older browsers its `sm:` and wider breakpoints don't apply (Chrome before 104, Safari before 16.4), and browsers without CSS cascade layers (Chrome before 99, Firefox before 97, Safari before 15.4) get no Tailwind styles at all. The extension's build target stays pinned to Chrome 102 and Firefox 115, its manifest minimums; Tailwind 4's CSS is designed for Chrome 111, Firefox 128 and Safari 16.4, and the extension gets fallbacks for the older browsers it supports. On touch-only devices, `hover:` styles no longer stick after a tap. The tinypool, vitest, `@vitest/mocker`, postcss-selector-parser and braces advisories no longer match either lock file. The CI rebuild of the extension from its AMO source zip now diffs every file in the Firefox package, not just `manifest.json`.

### Fixed

- Every surface reports the real package version. `/api/health` (local web UI and hosted demo) said `0.1`, the live-run User-Agent said `api-medic/0.1`, and `api_medic.__version__` was `0.1.0`, while the package was 1.1.1. The version now lives only in `src/api_medic/__init__.py`; `pyproject.toml` reads it at build time, and the CLI, web app, Lambda handler and runner import it. Release bumps now edit `__init__.py`. The hosted-demo Lambda copies the source without installing the package, which is why the version can't come from package metadata.
- The `auth.jwt.expired` finding says the expired token is "the most likely cause of the 401" only when the response is a 401. Before, it said so for any status, and for a captured request with no response.
- `deploy/README.md` says to redeploy with Run workflow on `main`, since re-running an old deploy run deploys that run's older commit. It and the comment in `deploy-demo.yml` also give the right reason the deploy's concurrency group is job-level.
- The frontend `HarUpload` and `RequestComposer` Re-run tests now wait for the second request instead of asserting the call count as soon as the already-rendered Report matched. The `HarUpload` one failed CI intermittently.

### Security

- Hosted demo `/api/run` now follows redirects by hand, at most 5 hops, and runs the SSRF guard (HTTPS only, public addresses only) on every hop's target before fetching it. Previously only the submitted URL was checked and `httpx` followed redirects on its own, so a public HTTPS URL could redirect the Lambda to plain HTTP, loopback, or an internal address. A blocked hop returns 400 naming the target and is never requested; a chain longer than 5 hops stops there and reports `http.redirect.too_many`, worded as a stopped chain rather than one that reached a final response. Because plain-HTTP hops are refused, `http.redirect.protocol_downgrade` can't fire on a hosted live run. Redirects within the cap produce the same Report as before. The CLI and `api-medic serve` are unchanged.
- The SSRF guard now rejects any address that isn't globally routable (`not ip.is_global`). This covers CGNAT `100.64.0.0/10` and other special-use ranges the named checks missed.
- `/api/run` also vets the submitted URL as `httpx` will send it. Python's socket module and `httpx` IDNA-encode some hostnames differently (`xß` becomes `xss` for one and `xn--x-qfa` for the other), so the guard could check a different name than the one fetched.
- `/api/run` returns 400 instead of 500 for a hostname `getaddrinfo` can't encode (e.g. a label over 63 characters) and for a URL `httpx` can't parse (e.g. a non-numeric port).
- The deploy role policy in `deploy/iam/inline-policy.json` is narrower. Its role statements now cover only SAM's generated function role (`role/api-medic-demo-ApiAnalyzeFunctionRole-*`) instead of every `api-medic-demo-*` role, so they can't touch the deploy role itself. On that role it allows `iam:AttachRolePolicy`/`DetachRolePolicy` only for `AWSLambdaBasicExecutionRole`, allows `iam:PassRole` only to Lambda, and drops `iam:PutRolePolicy`. Before, a workflow on `main` could create a role with `AdministratorAccess` and a Lambda function that used it. SAM's generated function role needs nothing more. The policy is applied to AWS by hand.
- The demo deploy workflow builds and deploys in separate jobs, so no step that installs or runs package code can reach the AWS deploy role. The build job (`npm ci`, the frontend build, `sam build`) has a read-only token, no `id-token: write` and no secrets, so its steps can't request an OIDC token. The deploy job assumes the role but runs no npm, pip or package code, and it runs on a fresh runner, so an `aws` or `sam` wrapper an install script puts on `$GITHUB_PATH` never reaches it. It deploys `deploy/template.yaml` and `deploy/samconfig.toml` from the commit, not the template `sam build` writes, and takes only the built Lambda directory and the site from the build job's artifact, after checking the artifact's digest and contents. Previously the builds ran in the deploy job with the role's keys in their environment; see `deploy/README.md`.
- Browser extension 0.2.0 masks credentials before **Analyze** sends a captured request to the hosted demo. `Authorization` keeps its scheme and whitespace; a Bearer JWT keeps its header and only its `exp`, `nbf` and `sub` claims, and its signature becomes `REDACTED`. Cookie values, secret-named headers (`X-Api-Key`, `X-Auth-Token`, ...), credential-like URL parameters (`token`, `code`, `api_key`, ...), a password in the URL, and secret fields in JSON and URL-encoded form bodies are masked with `*` characters of the same length. Browser-specific `_` fields (initiator stack traces, WebSocket messages) are dropped. `tests/unit/test_redaction_parity.py` proves the findings match on its masked and unmasked fixtures. `PRIVACY.md` and the hosted privacy page list what is masked. Previously the panel sent the captured request unmodified.
- Hosted demo `/api/run` now pins every connection (the TLS probe, the request and each redirect hop) to an address the SSRF guard checked in the same step, so a DNS-rebinding answer that changes after the check can't send it to an internal address. Host, SNI and certificate checks still use the hostname, and the live-run path ignores proxy environment variables. The guard also rejects IPv6 site-local `fec0::/10` and checks the IPv4 address inside IPv4-compatible and NAT64 addresses. `/api/run`, on the hosted demo and in `api-medic serve`, returns 400 with a message, before any DNS lookup, for input it used to answer with 500: a port outside 1-65535 (port 0 included) or not plain digits, square brackets around anything but an IPv6 address, an invalid `xn--` label, a hostname DNS can't encode, a non-string body, a non-ASCII method or header, or a lone surrogate. A redirect `Location` that can't be followed gives a Report with no response. The hosted demo's logs no longer carry request data: an unexpected error is logged by type and location only and answered with a plain 500, a rejected curl command's arguments aren't echoed to the log, and request URLs aren't logged.

## [1.1.1] - 2026-05-02

### Added

- HAR picker meta line on the hosted demo's HAR tab now surfaces uncompressed file size, capture date (from `log.pages[0].startedDateTime` or the first entry's `startedDateTime`), and unique-host count alongside the existing entry count. Fields are omitted when absent from the parsed HAR.
- Report action bar: `Re-run` re-fires whichever input produced the current Report (Run-tab composer state, HAR-tab uploaded file, Demos-tab fixture, or extension panel's captured DevTools entry). `Export markdown` copies a markdown rendering of the Report to the clipboard, mirroring the CLI's `--output markdown` format.

### Fixed

- Report action buttons (`Re-run`, `Export markdown`) are now wired up — they previously rendered but did nothing on click. `Share report` is intentionally hidden in v1: it requires persistence, which is out of scope.
- Lambda `/api/analyze` now returns 400 with a useful `detail` for malformed HAR entries (missing `request.method`, missing `request.url`, non-integer `response.status`, out-of-range status codes) instead of a silent 500. The HAR parser also validates required request fields up front rather than letting `KeyError` escape. This was hitting any browser-extension capture path that produced a partial entry — the panel previously surfaced "Analyze failed: 500 Internal Server Error" with no actionable hint.
- HAR parser error messages for `request.method` / `request.url` now distinguish *missing key*, *wrong type*, and *empty string* (previously all three collapsed into one "missing" message), and prefix the failure with `HAR entry[0] (<url>):` when the entry's URL is parseable. With multi-entry HARs the URL is what tells the user which captured request actually failed; the label degrades to `HAR entry[0]:` when the URL itself is the bad field.
- HAR parser now normalizes the response `httpVersion` field to the same form `httpx` produces in the live runner — `http/2.0`, `HTTP/2.0`, and `h2` all become `HTTP/2`; `http/1.1` becomes `HTTP/1.1`. Browser HARs disagree on casing and version notation (Chromium writes `http/2.0` lowercase, Firefox writes `HTTP/2.0` uppercase), which previously produced visually inconsistent Protocol values in the rendered Report depending on which surface produced it. Unknown values pass through unchanged.

## [1.1.0] - 2026-05-01

### Added

- New diagnostic check `http.redirect.too_many` (critical) — fires when a request's redirect chain has 5 or more hops. Catches misrouted servers and near-loops the cycle detector misses (e.g. distinct query strings on each hop). Brings the check catalog total to 20.

### Fixed

- CLI `run` no longer prints the report twice. The terminal renderer's rich Console was attached to `sys.stdout` while also recording into an export buffer; the CLI then echoed the buffered text. The Console is now backed by an in-memory file so only the CLI's `typer.echo` reaches stdout.
- CLI now reconfigures `sys.stdout` / `sys.stderr` to UTF-8 from both `cli_entry` and the Typer callback. Pip-generated `.exe` launchers built before the `cli_entry` entry point existed bypass `cli_entry` and would otherwise leave stdout on cp1252, mangling the renderers' arrow / em-dash glyphs on Windows.

## [1.0.0] - 2026-04-29

First public release. Three input surfaces (CLI, local web UI, hosted demo) sharing one core engine and a single `Report` shape.

### Added — Core engine

- Pydantic v2 data model: `Report`, `Finding`, `RequestSummary`, `ResponseSummary`, `TimingBreakdown`, `CapturedRequest`, `CapturedResponse`, `CapturedDns`, `CapturedTls`. `schema_version` bump-tracked.
- Parser: HAR 1.2 archives and curl command strings normalized to the same `CapturedRequest` shape.
- Runner: live HTTP execution via `httpx`, with `dnspython` pre-flight DNS probe and a separate `cryptography`-based TLS cert/protocol probe. Captures DNS / TLS / TTFB / total timing, redirect chain, raw headers (preserving duplicate `Set-Cookie`).
- Check battery — 19 diagnostic checks across:
  - **Network & transport** (6) — `network.dns.no_records`, `network.dns.slow`, `network.tls.expired`, `network.tls.expiring_soon`, `network.tls.weak_protocol`, `network.tls.cn_mismatch`
  - **HTTP semantics** (4) — `http.cors.misconfigured`, `http.headers.duplicate`, `http.redirect.loop`, `http.redirect.protocol_downgrade`
  - **Auth** (4) — `auth.missing`, `auth.jwt.expired`, `auth.jwt.not_yet_valid`, `auth.header.whitespace`
  - **Body / content** (3) — `body.malformed_json`, `body.content_length_mismatch`, `body.encoding_mismatch`
  - **Rate limiting** (2) — `rate_limit.hit` (429 with Retry-After context), `rate_limit.approaching` (X-RateLimit-Remaining < 10% of limit)
- Renderers: terminal (rich), JSON, Markdown, HTML — pluggable via `--output`.

### Added — CLI

- `api-medic <URL>` quick GET diagnosis (default subcommand).
- `api-medic run <URL>` full request with method, headers, body, auth.
- `api-medic from-curl '<command>'` parse + analyze (or just analyze) a curl command.
- `api-medic from-har <file.har>` analyze a captured HAR.
- `api-medic serve [--port 8765]` launch the local web UI.
- Output flags: `--output {terminal,json,markdown,html}`, `--save <path>`, `--no-color`, `--verbose`.

### Added — Local web UI

- Vite + React 18 + TypeScript + Tailwind, no design system library, no state management library.
- Three tabs: Demos (fixture browser), Run (request composer hitting `/api/run`), HAR (file upload hitting `/api/analyze`).
- TypeScript types auto-generated from Pydantic JSON Schema via `make types`; CI fails if regenerated types drift from the committed file.
- React bundle ships inside the Python wheel — no separate install step for the local UI.

### Added — Hosted demo (AWS)

- `https://api-medic.markandrewmarquez.com` on S3 + CloudFront + API Gateway HTTP API + Lambda.
- Routes: `POST /api/analyze` (captured-mode parser+engine), `POST /api/run` (live runner), `GET /api/health`.
- `/api/run` is SSRF-guarded (blocks RFC1918, link-local incl. EC2 metadata, multicast, loopback; HTTPS-only) with a 10-second per-request timeout.
- API Gateway throttling on `/api/run` (2 req/sec sustained, burst 5) plus a $10/month AWS Budget with 50%/100%/forecast email alerts.
- Lambda log group is stack-managed with 14-day retention.
- Same Report shape as the CLI and local web UI — byte-identical given the same input.
- Deploy via SAM, OIDC-based GitHub Actions role with least-privilege inline policy (committed at `deploy/iam/`).

### Added — Demo polish

- Client-side HAR stripping in three tiers (passthrough → strip request/response bodies → first-entry only) so 12 MB browser exports survive API Gateway's 10 MB body cap. When stripping fires, a banner alongside the rendered Report explains what was stripped.
- `VITE_DEMO_MODE=1` build path uses same-origin API base so CloudFront routes `/api/*` to the Lambda.

### Added — Quality

- 311 Python tests + 60 frontend tests.
- CI matrix: 3 operating systems × 3 Python versions = 9 cells.
- 80% coverage gate on `core/checks`.
- Static-import audit (`tests/unit/test_lambda_imports.py`) enforces Lambda dep boundaries — no fastapi/uvicorn/typer/rich, and explicit positive assertion that the Lambda surface DOES include the runner + SSRF guard so `/api/run` can't silently regress.
- Schema-sync gate: CI regenerates `frontend/src/lib/types.ts` from Pydantic and fails if it differs from the committed file.

### Added — Project

- 8 hand-crafted fixture reports covering distinct diagnostic scenarios; double as UI dev data, schema validation, and integration smoke tests.
- MIT license, public repo, full architecture spec at `docs/architecture.md`.

### Architectural decisions

- **Stateless by design.** No DynamoDB, no S3 writes for user data, no CloudWatch persistence beyond default logs.
- **No FastAPI / uvicorn on Lambda.** The Lambda dispatches routes inline via `lambda_handler` to keep cold-start budget intact even with the runner deps (httpx, dnspython, cryptography).
- **Same `Report` shape from every surface** — the contract that makes the future browser extension a thin capture layer rather than a parallel implementation.

[Unreleased]: https://github.com/marky224/api-medic/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/marky224/api-medic/releases/tag/v1.0.0
