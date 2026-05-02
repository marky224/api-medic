"""Parse external request representations into CapturedRequest.

Used by:
  * the local web UI's POST /api/analyze
  * the hosted demo's Lambda /api/analyze
  * the CLI's `from-har` and `from-curl` commands (Phase 4)

Doesn't execute anything — just normalises what the user provided. For live
execution see `core.runner`.
"""

from __future__ import annotations

import json
from typing import Any

import uncurl  # type: ignore[import-untyped]

from .captured import CapturedRequest, CapturedResponse
from .models import TimingBreakdown

# HAR `httpVersion` strings vary by browser: Chromium writes 'http/2.0' lowercase
# with .0, Firefox writes 'HTTP/2.0' uppercase with .0, and some tools use the
# ALPN identifier 'h2'. httpx's response.http_version is always 'HTTP/1.1' or
# 'HTTP/2' (uppercase, no .0 on h2). Normalising on parse keeps the rendered
# Report's Protocol field visually consistent regardless of which surface
# produced it. Unknown values pass through unchanged (key-missing in this map).
_HTTP_VERSION_NORMALIZATIONS = {
    "http/1.0": "HTTP/1.0",
    "http/1.1": "HTTP/1.1",
    "http/2": "HTTP/2",
    "http/2.0": "HTTP/2",
    "h2": "HTTP/2",
    "http/3": "HTTP/3",
    "http/3.0": "HTTP/3",
    "h3": "HTTP/3",
}


def _normalize_http_version(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return "HTTP/1.1"
    return _HTTP_VERSION_NORMALIZATIONS.get(raw.strip().lower(), raw)


def parse_har(raw: str | dict[str, Any]) -> CapturedRequest:
    """Parse a HAR 1.2 archive's first entry into a CapturedRequest.

    Multi-entry HARs are common (a full session capture); for v1 we analyse
    the first entry only.
    """
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or "log" not in data:
        raise ValueError("Not a HAR archive (missing 'log').")
    log = data["log"]
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("HAR has no entries.")
    entry = entries[0]
    if not isinstance(entry, dict) or "request" not in entry:
        raise ValueError("HAR entry is missing 'request'.")

    request = entry["request"]
    if not isinstance(request, dict):
        raise ValueError("HAR entry's 'request' must be an object.")

    # Best-effort URL extraction so field-error messages can identify which
    # captured request failed. Real-world HARs have many entries; v1 only
    # parses entries[0] but the URL is what tells the user *which* request
    # that was. Degrades to "HAR entry[0]" when url itself is the bad field.
    maybe_url = request.get("url")
    label = (
        f"HAR entry[0] ({maybe_url})"
        if isinstance(maybe_url, str) and maybe_url
        else "HAR entry[0]"
    )

    if "method" not in request:
        raise ValueError(f"{label}: request.method is missing.")
    method = request["method"]
    if not isinstance(method, str):
        raise ValueError(f"{label}: request.method must be a string, got {type(method).__name__}.")
    if not method:
        raise ValueError(f"{label}: request.method is empty.")

    if "url" not in request:
        raise ValueError("HAR entry[0]: request.url is missing.")
    url = request["url"]
    if not isinstance(url, str):
        raise ValueError(f"HAR entry[0]: request.url must be a string, got {type(url).__name__}.")
    if not url:
        raise ValueError("HAR entry[0]: request.url is empty.")

    request_headers = _har_headers(request.get("headers"))
    body_text = (request.get("postData") or {}).get("text", "")
    body = body_text.encode("utf-8") if isinstance(body_text, str) and body_text else b""

    captured_response: CapturedResponse | None = None
    response_obj = entry.get("response")
    if isinstance(response_obj, dict) and response_obj.get("status"):
        resp_headers = _har_headers(response_obj.get("headers"))
        resp_body_text = (response_obj.get("content") or {}).get("text", "")
        resp_body = (
            resp_body_text.encode("utf-8")
            if isinstance(resp_body_text, str) and resp_body_text
            else b""
        )
        try:
            status_code = int(response_obj["status"])
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"HAR entry's response.status is not an integer: {response_obj['status']!r}"
            ) from e
        status_text_raw = response_obj.get("statusText")
        captured_response = CapturedResponse(
            status_code=status_code,
            status_text=str(status_text_raw) if isinstance(status_text_raw, str) else "",
            headers=resp_headers,
            body=resp_body,
            protocol=_normalize_http_version(response_obj.get("httpVersion")),
        )

    timing = _timing_from_har(entry.get("timings") or {})

    return CapturedRequest(
        method=method.upper(),
        url=url,
        headers=request_headers,
        body=body,
        response=captured_response,
        timing=timing,
        source="har",
    )


def parse_curl(curl_str: str) -> CapturedRequest:
    """Parse a curl command string into a CapturedRequest.

    The curl command describes a request only — the resulting CapturedRequest
    has no `response`. Pair with `core.runner` to actually execute it.
    """
    if not curl_str.strip():
        raise ValueError("Empty curl command.")
    try:
        ctx = uncurl.parse_context(curl_str)
    except SystemExit as e:
        # uncurl uses argparse, which calls sys.exit() on parse failure.
        raise ValueError("Could not parse curl command (argparse rejected it).") from e
    except Exception as e:
        raise ValueError(f"Could not parse curl command: {e}") from e

    method = (ctx.method or "GET").upper()
    headers = dict(ctx.headers) if ctx.headers else {}
    body = ctx.data.encode("utf-8") if ctx.data else b""

    return CapturedRequest(
        method=method,
        url=ctx.url,
        headers=headers,
        body=body,
        response=None,
        timing=TimingBreakdown(),
        source="curl",
    )


def _har_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for h in raw:
        if isinstance(h, dict) and "name" in h and "value" in h:
            out[str(h["name"])] = str(h["value"])
    return out


def _timing_from_har(t: dict[str, Any]) -> TimingBreakdown:
    """HAR timings are in ms; -1 means 'not measured'."""

    def _opt(v: Any) -> float | None:
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
        return None

    dns = _opt(t.get("dns"))
    connect = _opt(t.get("connect"))
    ssl_ = _opt(t.get("ssl"))
    wait = _opt(t.get("wait"))
    receive = _opt(t.get("receive"))

    parts: list[float] = [v for v in (dns, connect, ssl_, wait, receive) if v is not None]
    total: float | None = sum(parts) if parts else None

    return TimingBreakdown(
        dns_ms=dns,
        connect_ms=connect,
        tls_ms=ssl_,
        ttfb_ms=wait,
        download_ms=receive,
        total_ms=total,
    )
