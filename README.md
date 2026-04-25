# api-medic

> **🚧 Under active development.**

A diagnostic tool for HTTP API issues. Captures or runs a request, checks it for common problems (network, transport, auth, protocol, body, rate limiting), and produces a structured report with plain-language findings and suggested fixes.

Built primarily for technical support engineers triaging customer-reported API issues, but useful for anyone debugging an HTTP integration.

## Status

Currently in **Phase 1: data model lock-in.** See [`docs/architecture.md`](docs/architecture.md) for the full build plan.

## Planned distribution

```bash
# Once v1 ships:
pip install api-medic
api-medic https://api.example.com/health
```

Hosted demo (captured-mode only): [api-medic.markandrewmarquez.com](https://api-medic.markandrewmarquez.com) — *not live yet.*

## License

MIT — see [LICENSE](LICENSE).
