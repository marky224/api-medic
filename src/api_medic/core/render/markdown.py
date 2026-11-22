"""Markdown renderer for Reports.

GitHub-flavored markdown. Designed to render cleanly when pasted into a
GitHub issue, a Slack message, or an email body. Severity is conveyed by
[CRITICAL]/[WARNING]/[INFO] tags rather than emoji. Text from the Report
is escaped so it reads as text (see _text, _cell and _code). The escaping
relies on each title, explanation and suggested fix starting with the
check's own words, since it doesn't guard the start of a line (`#`, `>`,
`-`, a fence, an indent).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Finding, Report, Severity, TimingBreakdown

_SEVERITY_TAG: dict[Severity, str] = {
    "critical": "[CRITICAL]",
    "warning": "[WARNING]",
    "info": "[INFO]",
}


def render_markdown(report: Report) -> str:
    parts: list[str] = []
    parts.append("# api-medic — diagnostic report")
    parts.append("")
    parts.append(
        _code(f"{report.request.method} {report.request.url}") + " → " + _status_str(report)
    )
    parts.append("")
    parts.append(_metrics_table(report))
    parts.append("")
    timing = _timing_table(report.timing)
    if timing:
        parts.append("## Timing")
        parts.append("")
        parts.append(timing)
        parts.append("")
    parts.append("## Findings")
    parts.append("")
    if not report.findings:
        parts.append("_No findings — the request looks healthy._")
    else:
        for f in report.findings:
            parts.append(_finding_block(f))
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _status_str(report: Report) -> str:
    if report.response is None:
        return "_no response_"
    return _code(f"{report.response.status_code} {report.response.status_text}")


def _metrics_table(report: Report) -> str:
    latency = _fmt_latency(report.timing.total_ms)
    body = _fmt_bytes(report.response.body_size_bytes) if report.response else "—"
    protocol = _cell(report.response.protocol) if report.response else "—"
    findings = _fmt_findings_count(report.findings)
    return (
        "| Latency | Body | Protocol | Findings |\n"
        "|---------|------|----------|----------|\n"
        f"| {latency} | {body} | {protocol} | {findings} |"
    )


def _timing_table(t: TimingBreakdown) -> str:
    rows: list[tuple[str, float]] = []
    for label, val in (
        ("DNS", t.dns_ms),
        ("Connect", t.connect_ms),
        ("TLS", t.tls_ms),
        ("TTFB", t.ttfb_ms),
        ("Download", t.download_ms),
    ):
        if val is not None:
            rows.append((label, val))
    if not rows and t.total_ms is None:
        return ""
    out = ["| Phase | Duration |", "|-------|---------:|"]
    for label, val in rows:
        out.append(f"| {label} | {val:.0f} ms |")
    if t.total_ms is not None:
        out.append(f"| **Total** | **{t.total_ms:.0f} ms** |")
    return "\n".join(out)


def _finding_block(f: Finding) -> str:
    lines = [
        f"### {_SEVERITY_TAG[f.severity]} {_text(f.title)}",
        f"**{_code(f.id)}**",
        "",
        _text(f.explanation),
    ]
    if f.evidence:
        lines.append("")
        lines.append("**Evidence:**")
        for k, v in f.evidence.items():
            lines.append(f"- {_code(k)}: {_code(_evidence_value(v))}")
    if f.suggested_fix:
        lines.append("")
        lines.append(f"**Suggested fix:** {_text(f.suggested_fix)}")
    return "\n".join(lines)


def _evidence_value(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if v is None:
        return "null"
    import json as _json

    return _json.dumps(v)


def _fmt_latency(total: float | None) -> str:
    if total is None:
        return "—"
    if total < 1000:
        return f"{total:.0f} ms"
    return f"{total / 1000:.2f} s"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n / (1024 * 1024):.1f} MB"


def _fmt_findings_count(findings: list[Finding]) -> str:
    if not findings:
        return "0"
    by_severity: dict[Severity, int] = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        by_severity[f.severity] += 1
    if by_severity["critical"]:
        return f"{by_severity['critical']} critical"
    if by_severity["warning"]:
        return f"{by_severity['warning']} warning"
    return f"{by_severity['info']} info"


_LINE_BREAKS = str.maketrans({"\r": "\\r", "\n": "\\n"})
_MARKUP = re.compile(r"\\(?=[!-/:-@\[-`{-~])|[\[\]<]|&(?=#?[0-9A-Za-z]+;)")
_BACKTICKS = re.compile(r"`+")


def _text(value: str) -> str:
    return _MARKUP.sub(_escape_markup, value.translate(_LINE_BREAKS))


def _escape_markup(match: re.Match[str]) -> str:
    c = match.group(0)
    if c == "<":
        return "&lt;"
    if c == "&":
        return "&amp;"
    return "\\" + c


def _cell(value: str) -> str:
    return _text(value).replace("|", "\\|")


def _code(value: str) -> str:
    value = value.translate(_LINE_BREAKS)
    fence = "`" * (max((len(run) for run in _BACKTICKS.findall(value)), default=0) + 1)
    ends = value[:1] + value[-1:]
    pad = " " if value.strip(" ") and ("`" in ends or ends == "  ") else ""
    return f"{fence}{pad}{value}{pad}{fence}"
