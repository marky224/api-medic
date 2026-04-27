"""AWS Lambda handler for the hosted demo.

The Lambda surface is captured-mode-only by architectural design — no
live runner, no httpx, no fastapi/uvicorn. This module imports only what
is safe in that environment: parser, engine, and the JSON renderer.

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

# Note: we deliberately don't `from api_medic.core.render import render_json`
# because that triggers core/render/__init__.py, which eager-imports the
# terminal renderer (and its `rich` dep) — bloating the Lambda zip. The
# JSON renderer is a thin wrapper around model_dump_json anyway, so we call
# it directly here.


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method, path = _extract_route(event)

    if method == "GET" and path == "/api/health":
        return _ok({"ok": True, "version": "0.1"})

    if method == "POST" and path == "/api/analyze":
        return _handle_analyze(event)

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
