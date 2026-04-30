# api-medic — v1 architecture spec

A diagnostic tool for HTTP API issues. Takes a request (live, or captured as a HAR file or curl command), runs it through a battery of network, transport, auth, and protocol checks, and produces a structured report with plain-language findings and suggested fixes.

This document is the build spec for v1. Mark Marquez directs the build; Claude implements alongside him. Public GitHub from commit 1, MIT license. Hosted demo at `api-medic.markandrewmarquez.com`.

---

## Done definition

v1 is shipped when:

- The README has a 30-second GIF showing the tool producing a useful report
- The hosted demo at `api-medic.markandrewmarquez.com` works on mobile
- `pip install api-medic` works on a clean Python 3.12 environment on macOS, Linux, and Windows
- A stranger can go from "never heard of it" to "just produced a useful report" in under 2 minutes

Every scope decision in this document is graded against that bar.

---

## Audience

Two surfaces, two voices:

- **README and other developer-facing copy** (PyPI page, GitHub, CLI help): written for developers debugging HTTP integrations. Terse, code-first, no persona pitch.
- **Portfolio article** (linked from job-search materials): written for recruiters reviewing applications for **technical support engineer (TSE)** roles. Frames the tool as something a TSE would reach for when triaging customer-reported API issues — that framing is load-bearing for recruiters and stays out of the README.

The tool itself is general-purpose. Other audiences — MSP/IT techs in customer environments, developers debugging their own integrations — use the same surface unchanged.

---

## Architecture overview

Three input surfaces (with a fourth coming post-launch), one shared core engine:

```mermaid
flowchart LR
    User((User))

    User --> CLI[CLI<br/>api-medic url]
    User --> LocalWeb[Local web UI<br/>api-medic serve]
    User --> Demo[Hosted demo<br/>captured + live]
    User -.v1.1.-> Ext[Browser extension<br/>DevTools panel]

    Demo --> CF[CloudFront + S3]
    CF --> APIGW[API Gateway]
    APIGW --> Lambda[Lambda]
    Ext -.v1.1.-> APIGW

    CLI --> Engine{{Core engine}}
    LocalWeb --> Engine
    Lambda --> Engine

    Engine --> Runner[Runner: live requests]
    Engine --> Parser[Parser: HAR / curl / raw HTTP]
    Engine --> Checks[Diagnostic checks]
    Engine --> Renderers[Renderers: terminal / json / md / html]
```

Key invariant: **the core engine produces byte-identical `Report` objects regardless of which surface invoked it,** given the same input. A report from the CLI, the local web UI, the hosted demo, or (eventually) the browser extension is interchangeable.

The Lambda surface uses Parser + Checks + Renderers + the live Runner, with SSRF mitigations on the Runner (block RFC1918, link-local incl. the EC2 metadata service, multicast, loopback; resolve DNS once and re-check the IP before the request). The browser extension is captured-mode-only by design — no privileged network access from a content script.

---

## Component breakdown

### Core engine (`api_medic/core`)

Pure Python library. No web, no CLI, no AWS dependencies. Importable and usable independently — this is what makes the same checks run identically across all surfaces.

Responsibilities:

- **Runner.** Given a request specification, execute it using `httpx` and capture full timing breakdown (DNS, connect, TLS, TTFB, download), cert chain details, negotiated protocol version, redirect chain. Used by CLI, local web UI, and the hosted-demo Lambda. The Lambda call path additionally goes through an SSRF guard (`core.runner_safety`) before any socket is opened.
- **Parser.** Given a HAR file, a curl command string, or a raw HTTP request/response pair, normalize into the same internal `CapturedRequest` representation that the Runner produces.
- **Check battery.** Run all applicable checks against a normalized request/response pair. Produce a list of `Finding` objects.
- **Renderers.** Convert a `Report` into terminal output, JSON, Markdown, or HTML.

### CLI (`api_medic/cli`)

Built with Typer. Single binary entry point: `api-medic`. Subcommands:

- `api-medic <URL>` — quick GET diagnosis (default behavior; no subcommand needed)
- `api-medic run <URL> [opts]` — full request with method, headers, body, auth
- `api-medic from-curl '<curl command>'` — parse and execute (or just analyze) a curl command
- `api-medic from-har <file.har>` — analyze a captured HAR file
- `api-medic serve [--port 8765]` — launch the local web UI
- `api-medic --version` / `api-medic --help`

Output flags: `--output {terminal,json,markdown,html}`, `--save <path>`, `--no-color`, `--verbose`.

### Local web UI (`api_medic/web`)

FastAPI app served by uvicorn. Bundled with the Python package — `api-medic serve` starts it locally at `http://localhost:8765`. The frontend is a pre-built React bundle shipped inside the Python wheel; no separate install step.

Routes:

- `GET /` — single-page React app
- `POST /api/run` — execute a request, return a `Report`
- `POST /api/analyze` — analyze a captured HAR or curl command, return a `Report`
- `GET /api/health` — liveness check

The local web UI supports both live and captured modes.

### Hosted demo (`deploy/`)

Both captured (HAR/curl) and live-run inputs are exposed. Architecture:

- **S3 + CloudFront.** Hosts the same React build as the local web UI. Subdomain `api-medic.markandrewmarquez.com` via CNAME pointing at the CloudFront distribution. ACM certificate must be issued in `us-east-1` regardless of where any other infrastructure lives — that's a CloudFront constraint.
- **API Gateway + Lambda.** Single Lambda function handling `POST /api/analyze`, `POST /api/run`, and `GET /api/health`. Wraps `api_medic.core` and runs the parser + check battery, plus the live Runner for `/api/run`. Returns the `Report` and exits.
- **Outbound HTTP is allowed but constrained.** `/api/run` goes through the SSRF guard (block RFC1918, link-local incl. 169.254.169.254, multicast, loopback; resolve hostname once, re-check the resolved IP, then connect via that IP) and a short per-request timeout (≤10s). Body and response size are capped well under the 10 MB API Gateway payload limit.
- **Cost / abuse controls.** API Gateway throttle on `/api/run` (low default; tunable in the SAM template), Lambda reserved concurrency on the function, AWS Budget at $10/month with 50%/100%/forecast alerts. The function is publicly callable; assume any URL the user submits will be fetched.
- **No DynamoDB, no S3 writes, no CloudWatch persistence beyond default 14-day log retention.** Stateless by design.

Lambda cold-start budget: 1.5s for the analyze-only path; the live-run path is allowed up to ~3s on cold start because of `httpx`'s import cost. `FastAPI` and `uvicorn` are still excluded — the Lambda dispatches routes inline via `lambda_handler`. The package now ships `httpx`, `dnspython`, and `cryptography` alongside `pydantic` and `uncurl`.

### Browser extension (post-v1, Phase 7)

Chrome/Firefox extension that adds a panel inside DevTools. Uses `chrome.devtools.network.onRequestFinished` to receive every request the user makes while DevTools is open. The user picks a captured request and clicks "Analyze with api-medic." The extension serializes the request to the same payload shape the hosted demo accepts, posts it to the existing `POST /api/analyze` endpoint, and renders the returned `Report` in the panel.

Notable design choices:

- **DevTools panel only**, not a background `webRequest` extension. The DevTools approach has a much smaller permission ask (no "this extension can read all your network traffic" warning), is opt-in by virtue of the user opening DevTools, and gives access to richer request data than a saved HAR file.
- **Server-side analysis.** The extension is a thin capture layer; the analysis runs on the same Lambda that the hosted demo uses. No diagnostic logic duplicated in JavaScript.
- **No new backend.** The extension reuses `POST /api/analyze`. Zero new infrastructure required when it ships.

This component is deferred to Phase 7 (post-launch) but the v1 architecture is already extension-ready — adding it later costs nothing today.

---

## Data model

The entire system is built around two Pydantic models. Get these right the first time; everything else flows from them.

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]

class Finding(BaseModel):
    id: str                    # stable check id, e.g. "auth.jwt.expired"
    severity: Severity
    title: str                 # plain-language headline
    explanation: str           # plain-language body
    evidence: dict | None      # structured raw data for the technical reader
    suggested_fix: str | None

class TimingBreakdown(BaseModel):
    dns_ms: float | None       # null for captured-mode where timing wasn't recorded
    connect_ms: float | None
    tls_ms: float | None
    ttfb_ms: float | None
    download_ms: float | None
    total_ms: float | None

class RequestSummary(BaseModel):
    method: str
    url: str
    headers: dict[str, str]
    body_size_bytes: int
    body_preview: str | None   # first ~500 chars, or null if binary

class ResponseSummary(BaseModel):
    status_code: int
    status_text: str
    headers: dict[str, str]
    body_size_bytes: int
    body_preview: str | None
    protocol: str              # "HTTP/1.1", "HTTP/2", etc.

class Report(BaseModel):
    id: str                    # uuid4
    schema_version: str        # "1.0"
    timestamp: datetime
    source: Literal["live", "har", "curl", "raw", "extension"]
    request: RequestSummary
    response: ResponseSummary | None
    timing: TimingBreakdown
    findings: list[Finding]
```

Findings are sorted by severity (critical first), then by check id alphabetically for deterministic output.

The `id` field on `Finding` is stable and namespaced (`network.dns.no_records`, `auth.jwt.expired`, `http.cors.misconfigured`). Stable ids let users filter, suppress, and reference specific findings without coupling to the human-readable title.

### TypeScript types

The frontend needs type definitions matching these Pydantic models exactly. Two reasonable paths:

1. **Generate from JSON Schema.** Pydantic's `Report.model_json_schema()` exports a JSON Schema; `quicktype` or `json-schema-to-typescript` converts that to TypeScript. Wire it as a `make types` target so the frontend types regenerate whenever the Python models change.
2. **Hand-maintain initially.** For v1's small surface area, hand-typing in `frontend/src/lib/types.ts` is fine. Add the codegen step later when the model surface grows.

Either way, the types in `frontend/src/lib/types.ts` are the contract the UI is built against in Phase 2.

---

## Fixtures

The fixtures-first build order (see Phased build plan below) means fixtures aren't an afterthought — they're a first-class artifact. They live in `tests/fixtures/reports/` and serve three purposes:

1. **UI development data source** in Phase 2, before the engine exists.
2. **Visual regression baseline** — every fixture should render identically across UI revisions.
3. **End-to-end test inputs** in Phase 3+, once the engine produces real Reports.

Eight to ten fixtures, each chosen to exercise a distinct scenario:

- `01-healthy.json` — clean response, only info-level findings (or none)
- `02-jwt-expired.json` — the canonical example, multiple critical findings
- `03-rate-limited.json` — 429 + Retry-After + X-RateLimit headers
- `04-cors-misconfigured.json` — preflight failure
- `05-tls-expiring.json` — cert expires in 5 days
- `06-malformed-body.json` — Content-Type says JSON, body is invalid
- `07-redirect-loop.json` — 3-redirect cycle
- `08-slow-tls.json` — performance issue, no auth/protocol error

Each fixture is a hand-crafted `Report` JSON file matching the Pydantic schema. They get checked into git and live in CI as smoke tests.

---

## Diagnostic checks for v1

Don't try to implement every possible HTTP check in v1. Pick a focused, high-impact set that covers the most common TSE ticket categories. v1 ships with these:

**Network & transport (6 checks)**

- `network.dns.no_records` — DNS lookup returned no A/AAAA records
- `network.dns.slow` — DNS resolution took >500ms (configurable)
- `network.tls.expired` — cert expiry already past
- `network.tls.expiring_soon` — cert expires inside the warning window (≤14 days)
- `network.tls.weak_protocol` — negotiated TLS < 1.2
- `network.tls.cn_mismatch` — cert subject doesn't match requested host

**HTTP semantics (4 checks)**

- `http.redirect.loop` — redirect chain has a cycle
- `http.redirect.protocol_downgrade` — HTTPS → HTTP redirect
- `http.cors.misconfigured` — CORS response headers contradict request origin
- `http.headers.duplicate` — duplicate or contradictory request/response headers

**Auth (4 checks)**

- `auth.missing` — 401 response, no Authorization header sent
- `auth.jwt.expired` — bearer token decoded as JWT, `exp` is in the past
- `auth.jwt.not_yet_valid` — JWT `nbf` claim is in the future
- `auth.header.whitespace` — Authorization value has leading/trailing whitespace or newlines

**Body / content (3 checks)**

- `body.malformed_json` — Content-Type says JSON, body fails to parse
- `body.content_length_mismatch` — Content-Length header doesn't match body length
- `body.encoding_mismatch` — Content-Encoding declared but body isn't actually encoded that way

**Rate limiting (2 checks)**

- `rate_limit.hit` — 429 status code
- `rate_limit.approaching` — `X-RateLimit-Remaining` is < 10% of limit

That's 19 checks. Each one is a small, testable Python function with a clear input contract and a `Finding | None` output. Adding more checks post-v1 is additive — drop a new function in `core/checks/` and register it.

A separate doc, `docs/checks.md`, should be maintained as the public catalog with one entry per check id explaining what it detects, why it matters, and what to do about it. This doubles as TSE study material — the kind of thing that gets shared.

---

## Repository structure

```
api-medic/
├── pyproject.toml              # build config, dependencies, entry points
├── Makefile                    # types codegen, test/lint/format shortcuts
├── README.md                   # primary marketing surface — must be excellent
├── LICENSE                     # MIT
├── CHANGELOG.md
├── PRIVACY.md                  # privacy policy (linked from hosted demo)
├── .github/
│   └── workflows/
│       ├── ci.yml              # tests, lint, type-check on PRs
│       ├── publish-pypi.yml    # auto-publish on tagged releases
│       └── deploy-demo.yml     # auto-deploy hosted demo on main
├── docs/
│   ├── architecture.md         # this document
│   └── examples/               # sample inputs + expected outputs
├── images/                     # README/marketing screenshots
├── store-assets/               # Chrome Web Store + AMO listing assets
├── src/
│   └── api_medic/
│       ├── __init__.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── models.py       # Pydantic models (Report, Finding, etc.)
│       │   ├── engine.py       # check registry + analyze() entry point
│       │   ├── runner.py       # live request execution via httpx
│       │   ├── runner_safety.py # SSRF guard for the Lambda live path
│       │   ├── parser.py       # HAR / curl / raw HTTP parsing
│       │   ├── captured.py     # CapturedRequest internal shape
│       │   ├── checks/
│       │   │   ├── __init__.py
│       │   │   ├── network.py
│       │   │   ├── http.py
│       │   │   ├── auth.py
│       │   │   ├── body.py
│       │   │   └── rate_limit.py
│       │   └── render/
│       │       ├── __init__.py
│       │       ├── terminal.py # rich-based pretty terminal output
│       │       ├── json.py
│       │       ├── markdown.py
│       │       └── html.py     # self-contained HTML (no external CSS)
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py         # Typer entry point
│       └── web/
│           ├── __init__.py
│           ├── app.py          # FastAPI app
│           ├── server.py       # uvicorn launcher for `api-medic serve`
│           └── frontend/       # built React static assets (gitignored;
│                               # populated at build time)
├── frontend/                   # React source (Vite)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── components/
│       │   ├── ReportView.tsx
│       │   ├── FindingCard.tsx
│       │   ├── TimingWaterfall.tsx
│       │   ├── RequestComposer.tsx
│       │   └── HarUpload.tsx
│       └── lib/
│           ├── api.ts
│           └── types.ts        # generated from Pydantic models
├── deploy/
│   ├── README.md               # how to deploy the hosted demo
│   ├── template.yaml           # AWS SAM template
│   └── lambda/
│       ├── Makefile            # SAM BuildMethod=makefile target
│       ├── requirements.txt    # Lambda runtime deps (httpx, dnspython, cryptography, pydantic)
│       └── handler.py          # wraps api_medic.core for Lambda (parser + checks + renderers + live Runner)
├── extension/                  # Phase 7 — browser extension (Vite + React 18, MV3)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── public/
│   │   ├── manifest.json           # Chrome MV3
│   │   └── manifest.firefox.json   # Firefox MV3
│   ├── scripts/
│   │   ├── package.mjs             # one zip per store
│   │   ├── package-source.mjs      # AMO source-zip generator
│   │   └── verify-dist.mjs
│   └── src/
│       ├── devtools.ts             # registers the panel
│       ├── panel.tsx               # panel entry point
│       ├── App.tsx
│       └── lib/
│           ├── api.ts              # POST /api/analyze
│           └── serialize.ts        # request → single-entry HAR
└── tests/
    ├── unit/
    │   ├── test_checks/
    │   ├── test_parser.py
    │   └── test_models.py
    ├── integration/
    │   └── test_cli.py
    └── fixtures/
        ├── reports/            # hand-crafted Report JSON files (Phase 1)
        ├── har/
        └── curl/
```

The frontend is its own Node project that builds into `src/api_medic/web/frontend/`. The Python package then ships those static assets inside the wheel. The same React build is uploaded to S3 for the hosted demo with `VITE_DEMO_MODE=1`, which switches the API base to same-origin so requests flow through CloudFront → API Gateway → Lambda. The flag also marks the build as the demo for cosmetic differences (tagline, future cost banners) — it does not hide any tabs, since the Lambda exposes both `/api/analyze` and `/api/run`.

---

## Tech stack

**Python side**
- Python 3.12 (target), 3.10+ (supported)
- `httpx` — request execution with timing introspection
- `dnspython` — explicit DNS resolution (separate from request)
- `cryptography` — cert parsing, JWT decoding (without verification — we just want to read claims)
- `pydantic` v2 — models, validation, JSON schema generation
- `typer` + `rich` — CLI framework + terminal rendering
- `fastapi` + `uvicorn` — local web server
- `pytest`, `ruff`, `mypy` — dev tooling

**Frontend**
- TypeScript + React 18
- Vite for bundling
- Tailwind CSS (no design system library — keep the bundle small and the look distinctive)
- No state management library; React's built-ins are enough for v1

**AWS / IaC**
- AWS SAM for the hosted demo
- us-east-1 (required for CloudFront ACM cert)
- CloudFront in front of S3 for the React build
- Custom domain via CNAME from `markandrewmarquez.com`'s DNS to the CloudFront distribution domain
- API Gateway HTTP API (cheaper and lower-latency than REST API) → Lambda
- Lambda runtime: Python 3.12, ~256 MB memory, 30s timeout

**CI/CD**
- GitHub Actions
- Three workflows: tests-on-PR, publish-to-PyPI-on-tag, deploy-demo-on-main

---

## Distribution

- **PyPI:** `pip install api-medic` — primary install path. Wheel includes the React build.
- **Hosted demo:** `https://api-medic.markandrewmarquez.com` — captured-mode + live-run (HTTPS only, SSRF-guarded, throttled). No install required.
- **Browser extension** (Phase 7): Chrome Web Store + Firefox Add-ons.

Not in v1: Homebrew formula, pre-built standalone binaries, Docker image. Add later if there's demand.

---

## Phased build plan

The build order is **fixtures first, UI against fixtures, then engine.** This produces a visible artifact early and prevents the engine from being shaped accidentally by frontend convenience.

**Phase 1 — Data model lock-in**

- Set up repo, `pyproject.toml`, CI skeleton, README placeholder
- Implement Pydantic models in `core/models.py`
- Generate or hand-write matching TypeScript types in `frontend/src/lib/types.ts`
- Hand-craft 8–10 fixture `Report` JSON files in `tests/fixtures/reports/` covering different scenarios and severities
- Write JSON schema validation tests that assert all fixtures parse cleanly

**End-of-phase demo:** A directory of fixture files; tests pass; both the Pydantic and TypeScript schemas exist.

**Phase 2 — Web UI against fixtures**

- Vite + React + Tailwind project skeleton in `frontend/`
- Build the report screen first (matches the mockup we already drew): metric cards, timing waterfall, finding cards
- Add the request composer (URL/method/headers/body form) and HAR upload pages
- Wire fixtures in as the data source — clicking "Run" loads a fixture file rather than calling an API
- Get mobile responsiveness right now, while the surface is small

**End-of-phase demo:** A working web UI, served locally, that looks finished. Every "report" is loaded from `fixtures/reports/`. Suitable for an early screenshot or LinkedIn post.

**Phase 3 — Core engine + parser**

- Implement `runner.py` with `httpx` and full timing capture
- Implement `parser.py` for HAR files and curl commands (use a battle-tested library like `uncurl`, don't write the curl parser from scratch)
- Implement all 19 checks
- Implement all four renderers (terminal, JSON, Markdown, HTML)
- Goal: the engine produces real `Report` objects byte-equivalent in shape to the fixtures
- FastAPI backend in `api_medic/web/app.py` exposing `/api/run` and `/api/analyze` — these replace the fixture-based data source in the frontend
- Tests: 80%+ coverage on the checks; integration test that runs the engine against a public test endpoint

**End-of-phase demo:** The same UI from Phase 2, now powered by real diagnostics. `api-medic serve` produces real reports against real endpoints.

**Phase 4 — CLI**

- Wrap the engine in Typer
- All subcommands: `run`, `from-curl`, `from-har`, `serve`
- Output flags wired up
- Integration tests using `typer.testing.CliRunner`

**End-of-phase demo:** `pip install -e . && api-medic https://httpbin.org/status/401` produces a colored terminal report.

**Phase 5 — Hosted demo on AWS**

- ACM certificate in `us-east-1` for `api-medic.markandrewmarquez.com`
- SAM template defining S3, CloudFront, API Gateway, Lambda
- Lambda handler in `deploy/lambda/handler.py` that wraps `api_medic.core` (parser + checks + renderers + live Runner with SSRF guard; excludes `fastapi`, `uvicorn`, and `rich`)
- Frontend build flag (`VITE_DEMO_MODE=1`) switches the API base to same-origin so CloudFront routes `/api/*` to API Gateway
- CNAME in `markandrewmarquez.com`'s DNS pointing `api-medic` at the CloudFront distribution
- GitHub Actions deploy workflow
- Mobile responsiveness pass (sanity check)

**End-of-phase demo:** `api-medic.markandrewmarquez.com` works on a phone.

**Phase 6 — Polish and launch**

- Record the 30-second README GIF (consider [vhs](https://github.com/charmbracelet/vhs) for a reproducible terminal recording, or QuickTime + ffmpeg for the web UI)
- README polish: hero section, install, quickstart, demo link, contributing, license
- PyPI release (v1.0.0), Docker Hub push
- Public launch: HN Show, LinkedIn post, dev.to blog, relevant subreddits, post in TSE communities
- Apply to first batch of TSE roles with the demo link in cover letter

**End-of-phase demo:** The done definition is met. v1 is shipped.

**Phase 7 (post-launch) — Browser extension**

- Manifest v3 setup (Chrome) and Firefox-compatible build
- DevTools panel UI built from the same React components used in the web UI (the `ReportView` component is reusable as-is)
- Capture handler using `chrome.devtools.network.onRequestFinished`
- "Analyze" action that posts the captured request to `https://api-medic.markandrewmarquez.com/api/analyze`
- CORS configuration on API Gateway with wildcard `AllowOrigins` (Firefox's per-install extension UUID can't be allowlisted by exact string, and HTTP API CORS only supports exact origins or `*`; safe here because no credentials are accepted and abuse is bounded by the per-route throttle, reserved concurrency, and AWS Budget)
- Chrome Web Store submission (review takes 1–3 weeks)
- Firefox Add-ons submission (faster review, same code)

**End-of-phase demo:** Open DevTools on any site, click the api-medic panel, pick a request, click Analyze, get a Report.

---

## Out of scope for v1

These are deferred to post-launch (Phase 7+) or later. None of them block the done definition.

- **History / share links.** Requires persistence. Stateless v1 is intentional.
- **Comparison mode.** "Diff this working request against this broken one." Compelling but adds significant UI surface area.
- **OpenAPI conformance.** Validating responses against an OpenAPI spec. High effort, narrower audience.
- **OAuth / SAML flow simulation.** Useful for auth-heavy SaaS but a major scope expansion.
- **WebSocket / SSE / streaming endpoint analysis.** Different transport, different check battery.
- **Plugin system for custom checks.** Worth doing eventually but premature for v1 — first prove the built-in checks land.
- **Pre-built binaries (Mac/Win/Linux).** PyInstaller / Briefcase. Add if `pip install` proves to be a friction point.
- **Homebrew formula.** Easy to add post-launch.

---

## Open questions / TODOs for the project

These need answers during the build but don't change the architecture. Most are Mark's calls (account access, distribution accounts) since they involve auth boundaries Claude can't cross:

1. AWS account: confirm which account the hosted demo will deploy into. If this is a personal account that also runs other things, set up an isolated IAM role or a separate sub-account for the api-medic deploy workflow.
2. DNS provider for `markandrewmarquez.com`: where the CNAME for the subdomain gets added. (Likely Cloudflare, Route 53, or your registrar's DNS.)
3. Default user-agent string for live requests: `api-medic/1.0 (+https://api-medic.markandrewmarquez.com)` is conventional and lets servers identify the tool.
4. Test endpoints for CI integration tests: `httpbin.org` and `postman-echo.com` are conventional but neither is bulletproof. Consider hosting a minimal test server in the `deploy/` stack.
5. Telemetry: should the tool collect anonymized usage data? Default answer: no. Opt-in only, ever, and only post-v1 if you decide it's worth the trust cost.
6. Browser extension distribution accounts: register Chrome Web Store developer ($5 one-time) and Firefox Add-ons accounts before Phase 7 — review timelines benefit from accounts being established early.

---

## Success criteria recap

If, in the period shortly after launch:

- The PyPI page shows a few hundred downloads
- A handful of GitHub stars and one or two issues from real users
- A recruiter mentions seeing the demo URL in your application
- You can demo the tool end-to-end in 90 seconds in an interview

…it's done its job as a portfolio piece. Anything beyond that is a bonus.
