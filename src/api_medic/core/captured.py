"""Internal types used by the engine before it produces a Report.

CapturedRequest is what Runner and Parser both produce. It carries full
request and response bodies and any extra metadata the checks need, before
the engine summarises it down into a public-shape Report (with truncated
body previews) for downstream consumers.

These types are deliberately separate from `models.py` — they're an internal
contract, not the public schema, and don't need TypeScript codegen.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .models import Source, TimingBreakdown


class CapturedResponse(BaseModel):
    """Full response captured by the Runner, or extracted from a HAR/raw
    HTTP source by the Parser. Body is the raw bytes; renderers truncate
    for display.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    status_code: int = Field(..., ge=100, le=599)
    status_text: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    protocol: str = "HTTP/1.1"


class CapturedRequest(BaseModel):
    """Full captured request + optional response, used by every check.

    `response` is None for inputs that don't include one (e.g. a curl
    command we haven't executed yet). Checks that need a response should
    no-op when it's missing.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""

    response: CapturedResponse | None = None
    timing: TimingBreakdown = Field(default_factory=TimingBreakdown)
    source: Source
