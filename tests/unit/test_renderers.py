"""Snapshot-style tests for terminal/markdown/html renderers.

We use a deterministic Report (fixed id and timestamp) so output is stable
across runs. Each test asserts presence of key elements rather than full
byte equality, which would force recompiling the test on every cosmetic
tweak.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from api_medic.core.engine import analyze
from api_medic.core.models import (
    Finding,
    Report,
    RequestSummary,
    ResponseSummary,
    TimingBreakdown,
)
from api_medic.core.parser import parse_curl, parse_har
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


# --- Markdown from the input ---------------------------------------------

# CommonMark, which GitHub's markdown extends, with GFM's tables and
# strikethrough. The renderer writes no link, image or HTML of its own, so
# any of them in its output came from the input.
_COMMONMARK = MarkdownIt("commonmark").enable(["table", "strikethrough"])
_IN_BACKTICKS = "`![t](https://evil.example/p.png)`"
# Each Report beside the markdown both renderers write for it;
# frontend/src/lib/markdown.test.ts renders them with markdown.ts.
_SHARED_CASES = sorted((Path(__file__).parent.parent / "fixtures" / "markdown").glob("*.json"))


def _markup(md: str) -> list[str]:
    """Each link, image and piece of HTML CommonMark reads in `md`."""
    found: list[str] = []
    for token in _COMMONMARK.parse(md):
        if token.type == "html_block":
            found.append(f"html_block {token.content}")
        for child in token.children or []:
            if child.type == "link_open":
                found.append(f"link_open {child.attrGet('href')}")
            elif child.type == "image":
                found.append(f"image {child.attrGet('src')}")
            elif child.type == "html_inline":
                found.append(f"html_inline {child.content}")
    return found


def _shown(inline: Token) -> str:
    """The text an inline token shows: its text and code spans."""
    return "".join(c.content for c in inline.children or [] if c.type in ("text", "code_inline"))


def _blocks(md: str, opener: str) -> list[str]:
    tokens = _COMMONMARK.parse(md)
    return [_shown(tokens[i + 1]) for i, token in enumerate(tokens) if token.type == opener]


def _headings(md: str) -> list[str]:
    return _blocks(md, "heading_open")


def _paragraphs(md: str) -> list[str]:
    return _blocks(md, "paragraph_open")


def _code_spans(md: str) -> list[str]:
    """What each code span in `md` shows."""
    return [
        child.content
        for token in _COMMONMARK.parse(md)
        for child in token.children or []
        if child.type == "code_inline"
    ]


def _metrics_rows(md: str) -> list[list[str]]:
    """The cells of the first table in `md`, row by row."""
    rows: list[list[str]] = []
    tokens = _COMMONMARK.parse(md)
    for i, token in enumerate(tokens):
        if token.type == "table_close":
            break
        if token.type == "tr_open":
            rows.append([])
        elif token.type in ("th_open", "td_open"):
            rows[-1].append(_shown(tokens[i + 1]))
    return rows


def _as_shown(value: str) -> str:
    """`value` as the renderer shows its line breaks: CR as \r, LF as \n."""
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _har_report(
    *,
    method: str = "GET",
    url: str = "https://api.example.com/v1",
    headers: dict[str, str] | None = None,
    status_text: str = "OK",
    protocol: str = "HTTP/1.1",
    response_headers: dict[str, str] | None = None,
) -> Report:
    """analyze() of a one-entry HAR whose 200 response took 10 ms."""

    def pairs(h: dict[str, str] | None) -> list[dict[str, str]]:
        return [{"name": k, "value": v} for k, v in (h or {}).items()]

    entry = {
        "request": {"method": method, "url": url, "headers": pairs(headers)},
        "response": {
            "status": 200,
            "statusText": status_text,
            "httpVersion": protocol,
            "headers": pairs(response_headers),
        },
        "timings": {"wait": 10},
    }
    return analyze(parse_har({"log": {"entries": [entry]}}))


_ORIGINS = {
    "link": "https://o.example [Re-authenticate here](https://evil.example/login)",
    "image": "https://o.example ![t](https://evil.example/p.png)",
    "html": (
        'https://o.example <img src="https://evil.example/p.png">'
        ' <a href="https://evil.example/">x</a>'
    ),
    "autolink": "https://o.example <https://evil.example/login>",
    "backslash-before-the-brackets": (
        "https://o.example \\[Re-authenticate here\\](https://evil.example/login)"
    ),
    "entities": "https://o.example &lt;b&gt; &#91;x&#93;",
}


class TestMarkdownShowsTheInputAsText:
    """A HAR file, a curl command or a server can put markdown in the text a
    report quotes. The renderer wrote it as it was, so wherever the report
    was pasted it could show a link, an image or HTML, or start a block.
    It escapes that text now, and CommonMark reads it back as given."""

    @pytest.mark.parametrize(
        ("origin", "allow"),
        [
            pytest.param(origin, allow, id=f"{allow_id}-{origin_id}")
            for allow_id, allow in (("no-allow", None), ("allow", "https://app.example"))
            for origin_id, origin in _ORIGINS.items()
        ],
    )
    def test_explanation_and_fix_show_the_origin_as_text(
        self, origin: str, allow: str | None
    ) -> None:
        response_headers = {"Access-Control-Allow-Origin": allow} if allow else None
        report = _har_report(headers={"Origin": origin}, response_headers=response_headers)
        (finding,) = report.findings
        out = render_markdown(report)
        assert _markup(out) == []
        assert finding.explanation in _paragraphs(out)
        assert f"Suggested fix: {finding.suggested_fix}" in _paragraphs(out)

    def test_emphasis_and_backticks_in_the_origin_are_left_as_they_are(self) -> None:
        # Only what makes a link, an image, HTML, an entity or a line break
        # is escaped, so these still format the text they wrap.
        report = _har_report(headers={"Origin": "https://o.example *x* _y_ ~z~ `w`"})
        (finding,) = report.findings
        out = render_markdown(report)
        assert f"\n{finding.explanation}\n" in out
        assert f"\n**Suggested fix:** {finding.suggested_fix}\n" in out

    def test_line_breaks_in_the_origin_add_no_block(self) -> None:
        # The reference definition would make the [CRITICAL] tag a link.
        origin = (
            "https://o.example\n\n# Pwned heading\n\n"
            "[critical]: https://evil.example/tag\r\n> quote"
        )
        report = _har_report(headers={"Origin": origin})
        (finding,) = report.findings
        out = render_markdown(report)
        assert _markup(out) == []
        assert _headings(out) == [
            "api-medic — diagnostic report",
            "Timing",
            "Findings",
            f"[CRITICAL] {finding.title}",
        ]
        assert _as_shown(finding.explanation) in _paragraphs(out)

    @pytest.mark.parametrize(
        "har",
        [
            pytest.param({"url": f"https://api.example.com/v1?q=a{_IN_BACKTICKS}"}, id="url"),
            pytest.param({"method": f"GET{_IN_BACKTICKS}"}, id="method"),
            pytest.param({"status_text": f"OK{_IN_BACKTICKS}"}, id="status-text"),
            pytest.param({"status_text": "`OK``"}, id="status-text-ending-in-backticks"),
            pytest.param(
                {"url": "https://api.example.com/v1\n# Pwned heading\n"}, id="url-line-break"
            ),
        ],
    )
    def test_request_and_status_lines_show_the_input_in_code(self, har: dict[str, Any]) -> None:
        report = _har_report(**har)
        out = render_markdown(report)
        assert _markup(out) == []
        assert _headings(out) == ["api-medic — diagnostic report", "Timing", "Findings"]
        if "status_text" in har:
            assert f"200 {har['status_text']}" in _code_spans(out)
        else:
            request_line = f"{report.request.method} {report.request.url}"
            assert _as_shown(request_line) in _code_spans(out)

    @pytest.mark.parametrize(
        "origin",
        [
            pytest.param(f"https://o.example{_IN_BACKTICKS}", id="image"),
            pytest.param("`https://o.example", id="starts-with-a-backtick"),
            pytest.param("https://o.example``", id="ends-with-backticks"),
            pytest.param(" https://o.example ", id="spaces-at-both-ends"),
            pytest.param("https://o.example\n# x", id="line-break"),
        ],
    )
    def test_evidence_value_is_shown_exactly(self, origin: str) -> None:
        out = render_markdown(_har_report(headers={"Origin": origin}))
        assert _markup(out) == []
        assert _as_shown(origin) in _code_spans(out)

    def test_json_evidence_is_shown_exactly(self) -> None:
        curl = f"curl https://api.example.com/ -H 'X-A: a{_IN_BACKTICKS}' -H 'X-A: b'"
        report = analyze(parse_curl(curl))
        (finding,) = report.findings
        assert finding.evidence is not None
        out = render_markdown(report)
        assert _markup(out) == []
        assert json.dumps(finding.evidence["duplicates"]) in _code_spans(out)

    @pytest.mark.parametrize(
        "protocol",
        [
            pytest.param("HTTP/1.1 | 0", id="pipe"),
            pytest.param("HTTP/1.1 |\n| evil | row | x | y |", id="line-break"),
            pytest.param("![t](https://evil.example/p.png)", id="image"),
            pytest.param("HTTP/1.1 \\| x", id="backslash-before-a-pipe"),
        ],
    )
    def test_protocol_cell_shows_the_protocol(self, protocol: str) -> None:
        # Beside a CORS finding, so the Findings cell isn't 0 already.
        report = _har_report(headers={"Origin": "https://o.example"}, protocol=protocol)
        out = render_markdown(report)
        assert _markup(out) == []
        assert _metrics_rows(out) == [
            ["Latency", "Body", "Protocol", "Findings"],
            ["10 ms", "0 B", _as_shown(protocol), "1 critical"],
        ]

    def test_title_and_evidence_key_show_the_input_as_text(self) -> None:
        # No check puts input in a title or an evidence key, so the Report is
        # built by hand: this fails if either stops being escaped.
        key = f"k{_IN_BACKTICKS}"
        report = Report.model_validate(
            {
                "source": "har",
                "request": {
                    "method": "GET",
                    "url": "https://api.example.com/",
                    "body_size_bytes": 0,
                },
                "timing": {},
                "findings": [
                    {
                        "id": "http.example.title",
                        "severity": "warning",
                        "title": "x [a](https://evil.example/)",
                        "explanation": "An explanation.",
                        "evidence": {key: "v"},
                    }
                ],
            }
        )
        out = render_markdown(report)
        assert _markup(out) == []
        assert "[WARNING] x [a](https://evil.example/)" in _headings(out)
        assert key in _code_spans(out)

    @pytest.mark.parametrize("case", _SHARED_CASES, ids=lambda p: p.stem)
    def test_shared_markdown_case(self, case: Path) -> None:
        # UTF-8 on every OS: each .md holds an em dash and an arrow.
        report = Report.model_validate_json(case.read_text(encoding="utf-8"))
        expected = case.with_suffix(".md").read_text(encoding="utf-8")
        assert render_markdown(report) == expected
        assert _markup(expected) == []

    def test_long_input_renders_quickly(self) -> None:
        # The escaping runs regexes on the input, which can be long. The
        # origin reaches the explanation, the suggested fix and an evidence
        # value.
        for value in (
            "\\[" * 100_000,
            "&a" * 100_000,
            "&" + "a" * 200_000,
            "`a" * 100_000,
            "\n" * 200_000,
        ):
            report = _har_report(headers={"Origin": value})
            started = time.perf_counter()
            out = render_markdown(report)
            elapsed = time.perf_counter() - started
            assert out.startswith("# api-medic — diagnostic report\n")
            # On Python 3.12+ a trace function, such as coverage's under CI's
            # --cov, slows Python code down, so the bound is checked only
            # when nothing is tracing.
            if sys.version_info < (3, 12) or sys.gettrace() is None:
                assert elapsed < 1, value[:10]
