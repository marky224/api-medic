"""HTML renderer for Reports.

Self-contained — no external CSS, no external fonts, no JS. The output is
a single `<html>` document you can save to disk, paste in an email, or
embed in an iframe. Visual hierarchy mirrors the React UI's design.
"""

from __future__ import annotations

from html import escape
from typing import Any

from ..models import Finding, Report, Severity, TimingBreakdown

_SEVERITY_BG: dict[Severity, str] = {
    "critical": "#fcebeb",
    "warning": "#faeeda",
    "info": "#e6f1fb",
}
_SEVERITY_FG: dict[Severity, str] = {
    "critical": "#a32d2d",
    "warning": "#854f0b",
    "info": "#185fa5",
}

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f4f3ee; color: #2c2c2a; margin: 0; padding: 32px 16px; line-height: 1.5; }
.wrap { max-width: 760px; margin: 0 auto; background: #ebe9e0; border-radius: 12px; padding: 20px; }
.card { background: #fff; border-radius: 12px; border: 0.5px solid rgba(0,0,0,.12); padding: 20px; }
h1 { font-size: 14px; font-weight: 500; margin: 0 0 16px; }
h2 { font-size: 13px; font-weight: 500; margin: 24px 0 10px; }
.req { display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px;
       background: #f1efe8; border-radius: 8px; margin-bottom: 20px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
.method { font-weight: 500; color: #185fa5; }
.metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 24px; }
.metric { background: #f1efe8; border-radius: 8px; padding: 10px 12px; }
.metric-label { font-size: 11px; color: #5f5e5a; margin-bottom: 4px; }
.metric-value { font-size: 18px; font-weight: 500; }
.timing-row { display: grid; grid-template-columns: 80px 1fr 60px; gap: 12px; align-items: center;
              font-size: 12px; margin-bottom: 6px; }
.timing-bar { height: 6px; background: #f1efe8; border-radius: 3px; overflow: hidden; }
.timing-bar-fill { height: 100%; background: #185fa5; }
.timing-value { text-align: right; color: #5f5e5a; font-variant-numeric: tabular-nums; }
.finding { background: #f1efe8; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
.finding-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 6px; align-items: center; }
.finding-title { font-weight: 500; font-size: 14px; }
.pill { font-size: 11px; padding: 2px 8px; border-radius: 8px; font-weight: 500; }
.explain { font-size: 13px; color: #5f5e5a; margin-bottom: 8px; }
.evidence { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px;
            background: #fff; padding: 6px 10px; border-radius: 8px; color: #5f5e5a;
            margin-bottom: 8px; border: 0.5px solid rgba(0,0,0,.08); }
.evidence-row { word-break: break-all; }
.fix { font-size: 12px; }
.fix-label { color: #5f5e5a; }
"""


def render_html(report: Report) -> str:
    parts: list[str] = [
        '<!doctype html><html lang="en"><head>',
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>api-medic report</title>",
        f"<style>{_CSS}</style></head><body>",
        '<div class="wrap"><div class="card">',
        "<h1>api-medic — diagnostic report</h1>",
    ]
    parts.append(_request_line(report))
    parts.append(_metrics(report))
    parts.append(_timing_block(report.timing))
    parts.append("<h2>Findings</h2>")
    if not report.findings:
        parts.append(
            '<p style="font-size:12px;color:#5f5e5a">No findings — the request looks healthy.</p>'
        )
    else:
        for f in report.findings:
            parts.append(_finding(f))
    parts.append("</div></div></body></html>")
    return "".join(parts)


def _request_line(report: Report) -> str:
    if report.response is None:
        status = '<span class="pill" style="background:#fcebeb;color:#a32d2d">No response</span>'
    else:
        bg = _SEVERITY_BG["critical"] if report.response.status_code >= 400 else "#e6f1fb"
        fg = _SEVERITY_FG["critical"] if report.response.status_code >= 400 else "#185fa5"
        status = (
            f'<span class="pill" style="background:{bg};color:{fg}">'
            f"{report.response.status_code} {escape(report.response.status_text)}</span>"
        )
    return (
        '<div class="req">'
        f'<span><span class="method">{escape(report.request.method)}</span> '
        f"{escape(report.request.url)}</span>{status}</div>"
    )


def _metrics(report: Report) -> str:
    latency = _fmt_latency(report.timing.total_ms)
    body = _fmt_bytes(report.response.body_size_bytes) if report.response else "—"
    protocol = report.response.protocol if report.response else "—"
    findings = _fmt_findings_count(report.findings)
    return (
        '<div class="metrics">'
        f'<div class="metric"><div class="metric-label">Latency</div><div class="metric-value">{latency}</div></div>'
        f'<div class="metric"><div class="metric-label">Body</div><div class="metric-value">{body}</div></div>'
        f'<div class="metric"><div class="metric-label">Protocol</div><div class="metric-value">{escape(protocol)}</div></div>'
        f'<div class="metric"><div class="metric-label">Findings</div><div class="metric-value">{findings}</div></div>'
        "</div>"
    )


def _timing_block(t: TimingBreakdown) -> str:
    rows = [
        (label, val)
        for label, val in (
            ("DNS", t.dns_ms),
            ("Connect", t.connect_ms),
            ("TLS", t.tls_ms),
            ("TTFB", t.ttfb_ms),
            ("Download", t.download_ms),
        )
        if val is not None
    ]
    if not rows:
        return ""
    denom = t.total_ms or sum(v for _, v in rows) or 1
    out = ["<h2>Timing breakdown</h2>"]
    for label, val in rows:
        pct = max(2, round(val / denom * 100))
        out.append(
            f'<div class="timing-row"><span style="color:#5f5e5a">{label}</span>'
            f'<div class="timing-bar"><div class="timing-bar-fill" style="width:{pct}%"></div></div>'
            f'<span class="timing-value">{val:.0f} ms</span></div>'
        )
    return "".join(out)


def _finding(f: Finding) -> str:
    bg, fg = _SEVERITY_BG[f.severity], _SEVERITY_FG[f.severity]
    parts = [
        '<div class="finding">',
        '<div class="finding-head">',
        f'<span class="finding-title">{escape(f.title)}</span>',
        f'<span class="pill" style="background:{bg};color:{fg}">{f.severity.title()}</span>',
        "</div>",
        f'<div class="explain">{escape(f.explanation)}</div>',
    ]
    if f.evidence:
        parts.append('<div class="evidence">')
        for k, v in f.evidence.items():
            parts.append(
                f'<div class="evidence-row">{escape(k)}: {escape(_evidence_value(v))}</div>'
            )
        parts.append("</div>")
    if f.suggested_fix:
        parts.append(
            f'<div class="fix"><span class="fix-label">Suggested fix:</span> '
            f"{escape(f.suggested_fix)}</div>"
        )
    parts.append("</div>")
    return "".join(parts)


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
