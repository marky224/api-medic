"""Body / content diagnostic checks.

Three checks against the response body and its declared metadata:
  * body.malformed_json — Content-Type JSON, body fails to parse
  * body.content_length_mismatch — Content-Length header vs actual byte length
  * body.encoding_mismatch — Content-Encoding declared, body lacks the magic
    bytes (gzip-only for v1; brotli and deflate have no reliable magic prefix)
"""

from __future__ import annotations

import json

from ..captured import CapturedRequest
from ..models import Finding
from . import register


@register
def malformed_json(captured: CapturedRequest) -> Finding | None:
    if captured.response is None:
        return None
    ct = _find(captured.response.headers, "Content-Type")
    if ct is None or "json" not in ct.lower():
        return None
    body = captured.response.body
    if not body:
        return None
    try:
        json.loads(body)
    except json.JSONDecodeError as e:
        preview = body[:120].decode("utf-8", errors="replace")
        return Finding(
            id="body.malformed_json",
            severity="critical",
            title="Response body is not valid JSON",
            explanation=(
                f"Content-Type declares JSON but the body fails to parse: {e.msg}. "
                "Most clients will throw an exception trying to deserialize this."
            ),
            evidence={
                "content_type": ct,
                "parse_error": str(e),
                "first_bytes": preview,
            },
            suggested_fix=(
                "Check the server output for stray HTML, BOM, or stack traces "
                "appended to the JSON. Fix the server."
            ),
        )
    return None


@register
def content_length_mismatch(captured: CapturedRequest) -> Finding | None:
    if captured.response is None:
        return None
    cl = _find(captured.response.headers, "Content-Length")
    if cl is None:
        return None
    try:
        declared = int(cl.strip())
    except ValueError:
        return None
    actual = len(captured.response.body)
    if declared == actual:
        return None

    # Content-Encoding-decoded bodies legitimately differ in length, as do
    # Transfer-Encoding: chunked responses.
    encoding = _find(captured.response.headers, "Content-Encoding")
    if encoding and encoding.strip().lower() not in ("identity", ""):
        return None
    if _find(captured.response.headers, "Transfer-Encoding"):
        return None

    return Finding(
        id="body.content_length_mismatch",
        severity="warning",
        title="Content-Length doesn't match body size",
        explanation=(
            f"The response header declares Content-Length: {declared}, but the "
            f"actual body is {actual} bytes. Strict clients may truncate or "
            "reject the response."
        ),
        evidence={
            "declared_bytes": declared,
            "actual_bytes": actual,
            "diff": actual - declared,
        },
        suggested_fix=(
            "On the server, set Content-Length to the actual byte length, or "
            "drop the header and let the transport infer the size."
        ),
    )


@register
def encoding_mismatch(captured: CapturedRequest) -> Finding | None:
    if captured.response is None:
        return None
    encoding = _find(captured.response.headers, "Content-Encoding")
    if not encoding:
        return None
    enc = encoding.strip().lower()
    body = captured.response.body
    if not body:
        return None

    # gzip is the only encoding with a reliable magic prefix. brotli and
    # deflate have no fixed prefix; skip them rather than false-positive.
    if enc == "gzip" and not body.startswith(b"\x1f\x8b"):
        return Finding(
            id="body.encoding_mismatch",
            severity="warning",
            title="Content-Encoding doesn't match body",
            explanation=(
                "The response declares Content-Encoding: gzip, but the body "
                "doesn't have the gzip magic bytes (1f 8b). Some clients will "
                "reject the response or display garbled output."
            ),
            evidence={
                "declared_encoding": "gzip",
                "first_bytes_hex": body[:8].hex(),
            },
            suggested_fix=(
                "Either remove the Content-Encoding header or actually compress "
                "the body before sending."
            ),
        )
    return None


def _find(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None
