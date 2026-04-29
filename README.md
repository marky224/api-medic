# api-medic

A diagnostic tool for HTTP API issues. Capture or run a request, get a structured report with plain-language findings — DNS, TLS, auth, CORS, rate limiting, body/encoding, redirects, the lot.

Built primarily for technical support engineers triaging customer-reported API issues, but useful for anyone debugging an HTTP integration.

## Try it now (no install)

[**api-medic.markandrewmarquez.com**](https://api-medic.markandrewmarquez.com) — paste a curl command, upload a HAR, or fire a live request from the browser. Live runs are HTTPS-only and throttled; captured-mode (curl/HAR) accepts anything.

## Install

```bash
pip install api-medic
```

Requires Python 3.10+.

## Quickstart

```bash
# Quickest possible diagnosis
api-medic https://api.example.com/v1/users

# Full request with method + headers + body
api-medic run https://api.example.com/v1/users \
    --method POST \
    --header "Authorization: Bearer ..." \
    --header "Content-Type: application/json" \
    --body '{"name": "Alex Doe"}'

# Analyze a curl command without re-running it
api-medic from-curl 'curl -X POST https://api.example.com/v1/users -H "Authorization: Bearer ..." -d ''{"name": "Alex Doe"}'''

# Analyze a HAR file (export from browser DevTools → Network → Save HAR)
api-medic from-har session.har

# Launch the local web UI on http://localhost:8765
api-medic serve
```

Output formats: `--output {terminal,json,markdown,html}`, default terminal.

## What gets checked

Twenty-plus diagnostic checks across:

- **Network:** DNS resolution, no records, address-class issues
- **TLS:** cert expiry, hostname mismatch, expiring soon, weak protocol
- **Transport:** redirect loops, redirect-to-http, slow TLS handshake
- **Auth:** JWT expiry, missing/malformed Authorization, suspicious signature
- **CORS:** preflight failures, origin not allowed, credentials misconfigured
- **Body:** malformed JSON, Content-Length mismatch, Content-Encoding mismatch
- **Rate limiting:** 429 with Retry-After surfaced as a finding
- **Status:** 4xx/5xx routing, server errors with body context

Every check produces the same `Report` shape — same fields, same JSON schema — whether it ran in the CLI, local web UI, or hosted demo. See [`docs/architecture.md`](docs/architecture.md) for the full check list and data model.

## Architecture

Three input surfaces, one core engine:

- **CLI** (`api-medic ...`): full feature set, terminal/JSON/MD/HTML output
- **Local web UI** (`api-medic serve`): same engine, browser frontend
- **Hosted demo** (`api-medic.markandrewmarquez.com`): captured + live, SSRF-guarded, throttled

Shared `Report` shape across all three surfaces — a CLI report and a hosted-demo report are byte-identical given the same input.

## Contributing

Issues and PRs welcome at [github.com/marky224/api-medic](https://github.com/marky224/api-medic). See [`docs/architecture.md`](docs/architecture.md) for the design rationale before proposing larger changes.

## License

MIT — see [LICENSE](LICENSE).
