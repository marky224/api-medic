"""Engine entry point: turn a CapturedRequest into a public-shape Report.

This is what every surface (web, CLI, Lambda) calls after Runner or Parser
produces a CapturedRequest. The output Report matches the shape Phase 1
fixtures define and the React UI consumes.

The body-preview truncation length matches the value implied by the Pydantic
model's `body_preview` docstring ("first ~500 chars").
"""

from __future__ import annotations

from .captured import CapturedRequest

# Import check modules so their @register decorators fire before analyze() is
# called. Add new modules here as they land in 3b.
from .checks import auth as _auth  # noqa: F401
from .checks import body as _body  # noqa: F401
from .checks import http as _http  # noqa: F401
from .checks import network as _network  # noqa: F401
from .checks import rate_limit as _rate_limit  # noqa: F401
from .checks import run_all_checks
from .models import Report, RequestSummary, ResponseSummary

_PREVIEW_CHARS = 500


def analyze(captured: CapturedRequest) -> Report:
    """Run all registered checks against the captured request and bundle a Report."""
    findings = run_all_checks(captured)
    return Report(
        source=captured.source,
        request=_summarize_request(captured),
        response=_summarize_response(captured),
        timing=captured.timing,
        findings=findings,
    )


def _summarize_request(captured: CapturedRequest) -> RequestSummary:
    return RequestSummary(
        method=captured.method,
        url=captured.url,
        headers=dict(captured.headers),
        body_size_bytes=len(captured.body),
        body_preview=_preview(captured.body),
    )


def _summarize_response(captured: CapturedRequest) -> ResponseSummary | None:
    if captured.response is None:
        return None
    r = captured.response
    return ResponseSummary(
        status_code=r.status_code,
        status_text=r.status_text,
        headers=dict(r.headers),
        body_size_bytes=len(r.body),
        body_preview=_preview(r.body),
        protocol=r.protocol,
    )


def _preview(body: bytes) -> str | None:
    """Return the first ~500 chars of body, or None if it's binary or empty."""
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[:_PREVIEW_CHARS]
