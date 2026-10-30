"""Snapshot-style tests for terminal/markdown/html renderers.

We use a deterministic Report (fixed id and timestamp) so output is stable
across runs. Each test asserts presence of key elements rather than full
byte equality, which would force recompiling the test on every cosmetic
tweak.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from api_medic.core.engine import analyze
from api_medic.core.models import (
    Finding,
    Report,
    RequestSummary,
    ResponseSummary,
    TimingBreakdown,
)
from api_medic.core.parser import parse_har
from api_medic.core.render import (
    render_html,
    render_json,
    render_markdown,
    render_terminal,
)


def _canonical_report() -> Report:
    return Report(
        id="00000000-0000-0000-0000-000000000002",
        timestamp=datetime(2026, 4, 25, 14, 23, 0, tzinfo=timezone.utc),
        source="live",
        request=RequestSummary(
            method="POST",
            url="https://api.example.com/v1/users",
            headers={"Authorization": "Bearer abc.def.ghi"},
            body_size_bytes=42,
            body_preview='{"name":"Alex Doe"}',
        ),
        response=ResponseSummary(
            status_code=401,
            status_text="Unauthorized",
            headers={"Content-Type": "application/json"},
            body_size_bytes=89,
            body_preview='{"error":"invalid_token"}',
            protocol="HTTP/2",
        ),
        timing=TimingBreakdown(
            dns_ms=12.0,
            connect_ms=45.0,
            tls_ms=78.0,
            ttfb_ms=95.0,
            download_ms=17.0,
            total_ms=247.0,
        ),
        findings=[
            Finding(
                id="auth.jwt.expired",
                severity="critical",
                title="Bearer token has expired",
                explanation="The JWT in your Authorization header expired 3h 0m ago.",
                evidence={
                    "exp": "2026-04-25T11:23:00Z",
                    "expired_for_seconds": 10800,
                    "sub": "user_123",
                },
                suggested_fix="Refresh the token at your token endpoint and retry.",
            )
        ],
    )


def _empty_report() -> Report:
    return Report(
        id="00000000-0000-0000-0000-000000000001",
        timestamp=datetime(2026, 4, 25, 14, 23, 0, tzinfo=timezone.utc),
        source="live",
        request=RequestSummary(
            method="GET",
            url="https://api.example.com/v1/health",
            body_size_bytes=0,
        ),
        response=ResponseSummary(
            status_code=200,
            status_text="OK",
            body_size_bytes=27,
            body_preview='{"status":"ok"}',
            protocol="HTTP/2",
        ),
        timing=TimingBreakdown(total_ms=120.0),
        findings=[],
    )


class TestRenderMarkdown:
    def test_includes_header_request_line_and_finding(self):
        out = render_markdown(_canonical_report())
        assert "# api-medic — diagnostic report" in out
        assert "POST https://api.example.com/v1/users" in out
        assert "401 Unauthorized" in out
        assert "[CRITICAL] Bearer token has expired" in out
        assert "`auth.jwt.expired`" in out
        assert "Refresh the token" in out
        assert "exp" in out
        assert "user_123" in out

    def test_metrics_table(self):
        out = render_markdown(_canonical_report())
        assert "| Latency | Body | Protocol | Findings |" in out
        assert "247 ms" in out
        assert "1 critical" in out
        assert "HTTP/2" in out

    def test_timing_table(self):
        out = render_markdown(_canonical_report())
        assert "## Timing" in out
        assert "DNS" in out
        assert "**Total**" in out

    def test_empty_findings_message(self):
        out = render_markdown(_empty_report())
        assert "_No findings" in out

    def test_no_response_handled(self):
        report = _canonical_report()
        report = report.model_copy(update={"response": None})
        out = render_markdown(report)
        assert "no response" in out.lower()


class TestRenderHtml:
    def test_self_contained_doctype_and_inline_styles(self):
        out = render_html(_canonical_report())
        assert out.startswith("<!doctype html>")
        assert "<style>" in out  # inline CSS, no link rel=stylesheet
        assert 'rel="stylesheet"' not in out
        assert "<script" not in out  # no JS

    def test_includes_request_method_and_url(self):
        out = render_html(_canonical_report())
        assert ">POST<" in out
        assert "https://api.example.com/v1/users" in out

    def test_critical_finding_uses_red_palette(self):
        out = render_html(_canonical_report())
        assert "#fcebeb" in out  # critical bg
        assert "#a32d2d" in out  # critical fg
        assert "Bearer token has expired" in out

    def test_escapes_html_in_user_strings(self):
        report = _canonical_report()
        report.findings[0].title = "<script>alert(1)</script>"
        out = render_html(report)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out


class TestRenderTerminal:
    def test_includes_method_url_and_finding(self):
        out = render_terminal(_canonical_report(), color=False)
        assert "POST" in out
        assert "https://api.example.com/v1/users" in out
        assert "401" in out
        assert "Bearer token has expired" in out
        assert "auth.jwt.expired" not in out  # title shown, id hidden in header

    def test_severity_label_in_panel_title(self):
        out = render_terminal(_canonical_report(), color=False)
        assert "CRITICAL" in out

    def test_evidence_rendered(self):
        out = render_terminal(_canonical_report(), color=False)
        assert "exp" in out
        assert "user_123" in out

    def test_empty_findings_message(self):
        out = render_terminal(_empty_report(), color=False)
        assert "No findings" in out

    def test_no_response_handled(self):
        report = _canonical_report()
        report = report.model_copy(update={"response": None})
        out = render_terminal(report, color=False)
        assert "no response" in out.lower()


class TestHarTimingTotalPastTheFloatMaximum:
    """Each phase of {"dns": 1e308, "wait": 1e308} is finite, but their sum
    isn't. The total showed as "inf s" and "inf ms" (JSON as null); every
    format now shows it as it shows a total that wasn't measured."""

    @pytest.mark.parametrize(
        "render",
        [render_json, lambda r: render_terminal(r, color=False), render_markdown, render_html],
        ids=["json", "terminal", "markdown", "html"],
    )
    def test_shown_as_not_measured(self, render: Callable[[Report], str]) -> None:
        entry = {
            "request": {"method": "GET", "url": "https://api.example.com/"},
            "timings": {"dns": 1e308, "wait": 1e308},
        }
        report = analyze(parse_har({"log": {"entries": [entry]}}))
        timing = report.timing.model_copy(update={"total_ms": None})
        assert render(report) == render(report.model_copy(update={"timing": timing}))
        # render_json wrote the old Infinity total as null too.
        assert report.timing.total_ms is None
