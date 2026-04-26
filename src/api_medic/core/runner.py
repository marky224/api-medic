"""Live HTTP request execution.

Used by:
  * the local web UI's POST /api/run
  * the CLI's `run` and bare-URL forms (Phase 4)

The hosted demo's Lambda surface deliberately does NOT use this — that
surface is captured-mode only.

Phase 3a populates `total_ms` via httpx's response clock. Per-phase
timing (DNS, connect, TLS, TTFB, download) and TLS cert introspection
are deferred to Phase 3b alongside the network/TLS checks that consume
that data.
"""

from __future__ import annotations

import time

import httpx

from .captured import CapturedRequest, CapturedResponse
from .models import TimingBreakdown

DEFAULT_USER_AGENT = "api-medic/0.1 (+https://api-medic.markandrewmarquez.com)"
DEFAULT_TIMEOUT_SECONDS = 30.0


def run(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> CapturedRequest:
    """Execute an HTTP request and capture the result.

    The `transport` kwarg is for tests (pass an `httpx.MockTransport`); it's
    not part of the public surface used by the web/CLI surfaces.

    Network failures (DNS, connection refused, timeout) produce a
    CapturedRequest with `response=None`. Callers should handle the
    no-response case (most diagnostic checks no-op for it).
    """
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    if body is None:
        body_bytes = b""
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body

    start = time.monotonic()
    response: httpx.Response | None = None
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=follow_redirects,
            transport=transport,
        ) as client:
            response = client.request(
                method=method,
                url=url,
                headers=request_headers,
                content=body_bytes if body_bytes else None,
            )
    except httpx.HTTPError:
        # Connection refused, DNS failure, timeout, etc. The CapturedRequest
        # still records what we tried to send and the elapsed time; checks
        # that need a response will skip themselves.
        response = None
    total_ms = (time.monotonic() - start) * 1000.0

    captured_response: CapturedResponse | None = None
    if response is not None:
        # response.headers.raw preserves original casing; .items() lowercases.
        # The Parser keeps casing too — keep both surfaces consistent.
        captured_response = CapturedResponse(
            status_code=response.status_code,
            status_text=response.reason_phrase or _default_reason(response.status_code),
            headers={k.decode("ascii"): v.decode("latin-1") for k, v in response.headers.raw},
            body=response.content,
            protocol=response.http_version,
        )

    return CapturedRequest(
        method=method.upper(),
        url=url,
        headers=request_headers,
        body=body_bytes,
        response=captured_response,
        timing=TimingBreakdown(total_ms=total_ms),
        source="live",
    )


def _default_reason(code: int) -> str:
    # httpx leaves reason_phrase empty for HTTP/2 responses. Fall back to a
    # sensible string so renderers always have something to show.
    if code < 200:
        return "Informational"
    if code < 300:
        return "OK"
    if code < 400:
        return "Redirect"
    if code < 500:
        return "Client Error"
    return "Server Error"
