"""Tests for core.render.json — thin wrapper around model_dump_json."""

from __future__ import annotations

import json

from api_medic.core.models import Finding, Report, RequestSummary, TimingBreakdown
from api_medic.core.render import render_json


def _report() -> Report:
    return Report(
        source="live",
        request=RequestSummary(
            method="GET",
            url="https://example.com/",
            headers={"Accept": "application/json"},
            body_size_bytes=0,
        ),
        response=None,
        timing=TimingBreakdown(total_ms=120.0),
        findings=[
            Finding(
                id="auth.jwt.expired",
                severity="critical",
                title="Bearer token has expired",
                explanation="The JWT expired 1h ago.",
                evidence={"exp": "2026-04-25T11:23:00Z"},
                suggested_fix="Refresh the token.",
            )
        ],
    )


class TestRenderJson:
    def test_returns_indented_json_by_default(self):
        out = render_json(_report())
        assert "\n" in out  # indented = newlines
        assert "  " in out

    def test_compact_when_indent_none(self):
        out = render_json(_report(), indent=None)
        assert "\n" not in out

    def test_round_trip_via_pydantic(self):
        original = _report()
        rendered = render_json(original)
        parsed = json.loads(rendered)
        # Reconstruct via Pydantic and compare key fields.
        rebuilt = Report.model_validate(parsed)
        assert rebuilt.source == original.source
        assert rebuilt.request.url == original.request.url
        assert len(rebuilt.findings) == 1
        assert rebuilt.findings[0].id == "auth.jwt.expired"

    def test_includes_schema_version(self):
        parsed = json.loads(render_json(_report()))
        assert parsed["schema_version"] == "1.0"
