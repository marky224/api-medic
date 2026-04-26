"""Internal types used by the engine before it produces a Report.

CapturedRequest is what Runner and Parser both produce. It carries full
request and response bodies and any extra metadata the checks need, before
the engine summarises it down into a public-shape Report (with truncated
body previews) for downstream consumers.

These types are deliberately separate from `models.py` — they're an internal
contract, not the public schema, and don't need TypeScript codegen.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import Source, TimingBreakdown


class CapturedDns(BaseModel):
    """DNS lookup result for the request's host. None when DNS wasn't probed
    (HAR/curl sources, or live runs with probing disabled)."""

    model_config = ConfigDict(extra="forbid")

    records: list[str] = Field(
        default_factory=list, description="Resolved A/AAAA records (IPv4 + IPv6)."
    )


class CapturedTls(BaseModel):
    """TLS handshake details — populated only for HTTPS live runs. None for
    HAR, curl, and HTTP-scheme requests."""

    model_config = ConfigDict(extra="forbid")

    not_before: datetime
    not_after: datetime
    subject_common_name: str | None = None
    subject_alt_names: list[str] = Field(default_factory=list)
    issuer_common_name: str | None = None
    negotiated_protocol_version: str = Field(default="", description="e.g. 'TLSv1.2', 'TLSv1.3'.")


class CapturedResponse(BaseModel):
    """Full response captured by the Runner, or extracted from a HAR/raw
    HTTP source by the Parser. Body is the raw bytes; renderers truncate
    for display.

    `raw_headers` is the original (name, value) sequence preserving
    duplicates and case. None when the source didn't preserve them
    (Phase 3a Runner output, for instance — populated in 3b-F).
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    status_code: int = Field(..., ge=100, le=599)
    status_text: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    raw_headers: list[tuple[str, str]] | None = None
    body: bytes = b""
    protocol: str = "HTTP/1.1"


class CapturedRequest(BaseModel):
    """Full captured request + optional response, used by every check.

    `response` is None for inputs that don't include one (e.g. a curl
    command we haven't executed yet). Checks that need a response should
    no-op when it's missing.

    `raw_headers` mirrors CapturedResponse.raw_headers — original
    (name, value) sequence with duplicates preserved.

    `redirect_chain` is the list of URLs visited starting from the
    user-supplied URL and ending at the final one. None when not captured
    (HAR/curl don't carry it; Runner populates in 3b-F).
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    raw_headers: list[tuple[str, str]] | None = None
    body: bytes = b""

    response: CapturedResponse | None = None
    timing: TimingBreakdown = Field(default_factory=TimingBreakdown)
    redirect_chain: list[str] | None = None
    dns: CapturedDns | None = None
    tls: CapturedTls | None = None
    source: Source
