# Changelog

All notable changes to api-medic are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
