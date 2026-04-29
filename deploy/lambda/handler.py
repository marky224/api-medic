"""AWS Lambda handler for the hosted demo.

Routes:
  GET  /api/health   liveness check
  POST /api/analyze  parse + check a captured HAR or curl command
  POST /api/run      execute a live HTTP request, then check it

The /api/run path goes through `core.runner_safety` first to block
SSRF (RFC1918, link-local incl. EC2 metadata, multicast, loopback).
A 10s per-request timeout caps cost on slow targets. API Gateway
throttling and Lambda reserved concurrency provide the broader
abuse / cost ceiling — see deploy/template.yaml.

FastAPI / uvicorn are deliberately excluded (route dispatch is inline
below). The terminal renderer's `rich` dep is also excluded — we
serialize the Report via model_dump_json directly.

Triggered by API Gateway HTTP API v2 events. CloudFront proxies /api/*
to this Lambda; same-origin with the React build, so no CORS headers
are needed in the response.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from api_medic.core.engine import analyze
from api_medic.core.parser import parse_curl, parse_har
from api_medic.core.runner import run as run_request
from api_medic.core.runner_safety import UnsafeURLError, check_url_safe

# Hard cap per the architecture invariants: a single live request can't
# exceed this regardless of httpx defaults. Bounded so the Lambda doesn't
# burn its 30s ceiling on a slow target.
LIVE_RUN_TIMEOUT_SECONDS = 10.0


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method, path = _extract_route(event)

    if method == "GET" and path == "/api/health":
        return _ok({"ok": True, "version": "0.1"})

    if method == "POST" and path == "/api/analyze":
        return _handle_analyze(event)

    if method == "POST" and path == "/api/run":
        return _handle_run(event)

    if method == "OPTIONS":
        # API Gateway should already handle CORS preflight; this is a
        # belt-and-braces fallback.
        return _ok({})

    return _err(404, f"Unknown route: {method} {path}")


def _handle_analyze(event: dict[str, Any]) -> dict[str, Any]:
    raw = _decode_body(event)

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        return _err(400, f"Body is not valid JSON: {e.msg}")

    if not isinstance(body, dict) or "kind" not in body:
        return _err(400, "Body must include 'kind' (one of 'har', 'curl').")

    kind = body["kind"]
    try:
        if kind == "har":
            har_payload = body.get("har")
            if har_payload is None:
                return _err(400, "Missing 'har' field for kind=har.")
            captured = parse_har(har_payload)
        elif kind == "curl":
            curl = body.get("curl", "")
            if not curl:
                return _err(400, "Missing 'curl' field for kind=curl.")
            captured = parse_curl(curl)
        else:
            return _err(400, f"Unknown kind: {kind!r} (expected 'har' or 'curl').")
    except ValueError as e:
        return _err(400, str(e))

    report = analyze(captured)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": report.model_dump_json(),
    }


def _handle_run(event: dict[str, Any]) -> dict[str, Any]:
    raw = _decode_body(event)

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        return _err(400, f"Body is not valid JSON: {e.msg}")

    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object.")

    method = body.get("method")
    url = body.get("url")
    headers = body.get("headers") or {}
    req_body = body.get("body")

    if not isinstance(method, str) or not method:
        return _err(400, "Missing or empty 'method'.")
    if not isinstance(url, str) or not url:
        return _err(400, "Missing or empty 'url'.")
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        return _err(400, "'headers' must be a dict[str, str].")

    try:
        check_url_safe(url)
    except UnsafeURLError as e:
        return _err(400, str(e))

    captured = run_request(
        method=method,
        url=url,
        headers=headers,
        body=req_body,
        timeout=LIVE_RUN_TIMEOUT_SECONDS,
        probe_network=True,
    )
    report = analyze(captured)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": report.model_dump_json(),
    }


def _extract_route(event: dict[str, Any]) -> tuple[str, str]:
    """API Gateway HTTP API v2 event format."""
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath") or http.get("path", "")
    return method, path


def _decode_body(event: dict[str, Any]) -> str:
    raw = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
        except (ValueError, base64.binascii.Error):
            return ""
    return raw


def _ok(payload: Any) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _err(status: int, detail: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"detail": detail}),
    }
