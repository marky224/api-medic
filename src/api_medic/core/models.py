"""Pydantic data models for api-medic.

These models are the contract every surface (CLI, local web, hosted demo, browser
extension) produces and consumes. The TypeScript types in
`frontend/src/lib/types.ts` are generated from these via `make types`.

The shapes here match the architecture spec exactly. Changes to these models are
schema-breaking and require a `schema_version` bump.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["info", "warning", "critical"]
"""Severity levels for findings, ordered low → high."""

Source = Literal["live", "har", "curl", "raw", "extension"]
"""Where a Report came from. 'live' = Runner executed it; everything else is parsed."""

# Sort order for severities — critical first, then warning, then info.
# Lower number = higher severity = sorted first.
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


class Finding(BaseModel):
    """A single diagnostic finding produced by one check.

    Findings have three layers (matching the UI design):
      - `title`: plain-language headline for non-technical readers
      - `evidence`: structured raw data for technical readers
      - `suggested_fix`: actionable next step
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        description=(
            "Stable, namespaced check identifier "
            "(e.g. 'auth.jwt.expired', 'network.dns.no_records')."
        ),
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
    )
    severity: Severity
    title: str = Field(..., min_length=1, description="Plain-language headline.")
    explanation: str = Field(..., min_length=1, description="Plain-language body.")
    evidence: dict[str, Any] | None = Field(
        default=None,
        description="Structured raw data for the technical reader. Optional.",
    )
    suggested_fix: str | None = Field(
        default=None,
        description="Action to take. Optional — not every finding has a clear fix.",
    )


class TimingBreakdown(BaseModel):
    """Per-phase request timing in milliseconds.

    Every field is nullable: captured-mode reports (HAR, curl) may not have
    full per-phase timing data.
    """

    model_config = ConfigDict(extra="forbid")

    dns_ms: float | None = Field(default=None, ge=0)
    connect_ms: float | None = Field(default=None, ge=0)
    tls_ms: float | None = Field(default=None, ge=0)
    ttfb_ms: float | None = Field(default=None, ge=0)
    download_ms: float | None = Field(default=None, ge=0)
    total_ms: float | None = Field(default=None, ge=0)


class RequestSummary(BaseModel):
    """Captured snapshot of the request that was sent (or would have been sent)."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(..., min_length=1, description="HTTP method, uppercase.")
    url: str = Field(..., min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    body_size_bytes: int = Field(..., ge=0)
    body_preview: str | None = Field(
        default=None,
        description="First ~500 chars, or null if binary.",
    )


class ResponseSummary(BaseModel):
    """Captured snapshot of the response received."""

    model_config = ConfigDict(extra="forbid")

    status_code: int = Field(..., ge=100, le=599)
    status_text: str = Field(..., description="e.g. 'OK', 'Unauthorized'.")
    headers: dict[str, str] = Field(default_factory=dict)
    body_size_bytes: int = Field(..., ge=0)
    body_preview: str | None = Field(default=None)
    protocol: str = Field(
        ...,
        description="Negotiated protocol, e.g. 'HTTP/1.1', 'HTTP/2'.",
    )


class Report(BaseModel):
    """The single artifact produced by every api-medic surface.

    A Report from the CLI, the local web UI, the hosted demo, or the browser
    extension is byte-identical given the same input. Don't break that.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    schema_version: str = Field(default="1.0")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Source
    request: RequestSummary
    response: ResponseSummary | None = Field(
        default=None,
        description="Null when the request never completed (DNS failure, connect timeout, etc).",
    )
    timing: TimingBreakdown
    findings: list[Finding] = Field(default_factory=list)

    @field_validator("findings")
    @classmethod
    def _sort_findings(cls, v: list[Finding]) -> list[Finding]:
        """Sort findings: critical → warning → info, then by id alphabetically.

        This guarantees deterministic output for every renderer and downstream
        consumer. Anyone constructing a Report can pass findings in any order.
        """
        return sorted(v, key=lambda f: (_SEVERITY_RANK[f.severity], f.id))
