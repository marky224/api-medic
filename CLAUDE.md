# CLAUDE.md — operating brief for Claude Code in this repo

This is api-medic, an open-source HTTP API troubleshooting tool. Mark Marquez directs the build; Claude implements alongside him.

## Source of truth
- `docs/architecture.md` — the full v1 build spec. Tech stack, repo structure, data model, invariants, phases. Consult actively.
- The Pydantic models in `src/api_medic/core/models.py` are the contract every surface produces and consumes. Don''t break them without bumping `schema_version`.
- `frontend/src/lib/types.ts` is auto-generated from those models via `make types`. Never edit by hand.

## Current phase
Phase 2 — Web UI against fixtures. Vite + React + TypeScript + Tailwind in `frontend/`. Build the report screen first (matching the design notes in the architecture doc and `api-medic-ui-reference.html` if Mark provides it). Wire fixtures from `tests/fixtures/reports/` as the data source — no real API calls in this phase.

## Working style
- Default to producing actual code, not pseudocode.
- Write tests alongside the code.
- Push back when you disagree.
- One question at a time when scoping.
- Don''t expand scope without flagging.
- No emojis unless Mark uses them first.
- Skip sycophantic openers ("Great question!", etc.).

## Architectural invariants (don''t change without sign-off)
- Same Report shape from every surface (CLI, web, Lambda, extension).
- Lambda surface ships parser + checks + renderers + the live runner.
  Outbound HTTP from the Lambda is allowed and must apply SSRF mitigations
  (block RFC1918, link-local incl. 169.254.169.254 metadata, multicast,
  loopback; resolve hostname once and re-check the IP before connecting)
  plus a conservative per-request timeout (≤10s). FastAPI and uvicorn
  remain excluded — the Lambda dispatches routes inline via
  `lambda_handler`, no web framework.
- Hosted demo exposes both captured-mode (HAR/curl) and live-run tabs.
  Live runs are bounded by API Gateway throttling, Lambda reserved
  concurrency, and the AWS Budget; assume any URL the user submits
  will be fetched by the Lambda.
- Stateless: no DynamoDB, no S3 writes for user data, no persistence.
- Public repo, MIT license, no public timelines anywhere.

## Tech stack (locked)
- Python 3.10+ (3.12 target), httpx, dnspython, cryptography, pydantic v2, typer, rich, fastapi, uvicorn
- TypeScript + React 18 + Vite + Tailwind. No design system library. No state management library.
- AWS SAM in us-east-1, API Gateway HTTP API → Lambda, CloudFront + S3
- GitHub Actions CI

## Quality gates (must pass before pushing)
- `pytest` — all green
- `ruff check .` and `ruff format --check .`
- `mypy` — clean
- `make types` produces no diff vs committed `types.ts` (also verified by CI)

## Out of scope for v1
History/share links, comparison mode, OpenAPI validation, OAuth simulation, WebSocket support, plugin system, pre-built binaries, telemetry. Don''t build these unless Mark explicitly asks.

## Audience
README and marketing copy frame this as a tool for technical support engineers triaging customer-reported API issues. Tool itself is general-purpose.
