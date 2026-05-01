"""Terminal renderer for Reports.

Uses `rich` to produce a colored, scannable layout in the user's terminal.
The CLI's --no-color flag flips the `color` argument to False so output
behaves well in pipes and CI logs.
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..models import Finding, Report, Severity, TimingBreakdown

_SEVERITY_STYLE: dict[Severity, str] = {
    "critical": "bold red",
    "warning": "bold yellow",
    "info": "bold blue",
}

_SEVERITY_LABEL: dict[Severity, str] = {
    "critical": "CRITICAL",
    "warning": "WARNING",
    "info": "INFO",
}


def render_terminal(report: Report, *, color: bool = True, width: int | None = 88) -> str:
    """Render to a string using a Console with `record=True`.

    The Console writes into an in-memory buffer rather than the real stdout —
    callers are responsible for emitting the returned text. Without this, every
    `console.print(...)` would also land on stdout and the CLI's subsequent
    `typer.echo(text)` would render the report a second time.
    """
    console = Console(
        record=True,
        width=width,
        no_color=not color,
        force_terminal=color,
        file=io.StringIO(),
    )
    console.print(_request_line(report))
    console.print(_metrics_table(report))
    timing = _timing_table(report.timing)
    if timing is not None:
        console.print(timing)
    if not report.findings:
        console.print(Padding("[dim]No findings — the request looks healthy.[/dim]", (1, 0, 0, 0)))
    else:
        console.print()
        for f in report.findings:
            console.print(_finding_panel(f))
    return console.export_text(clear=False, styles=color)


def _request_line(report: Report) -> Text:
    line = Text()
    line.append(report.request.method, style="bold blue")
    line.append(" ")
    line.append(report.request.url)
    line.append("  →  ")
    if report.response is None:
        line.append("no response", style="bold red")
    else:
        style = "bold red" if report.response.status_code >= 400 else "bold green"
        line.append(f"{report.response.status_code} {report.response.status_text}", style=style)
    return line


def _metrics_table(report: Report) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Latency", _fmt_latency(report.timing.total_ms))
    t.add_row(
        "Body",
        _fmt_bytes(report.response.body_size_bytes) if report.response else "—",
    )
    t.add_row("Protocol", report.response.protocol if report.response else "—")
    t.add_row("Findings", _fmt_findings_count(report.findings))
    return t


def _timing_table(t: TimingBreakdown) -> Table | None:
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
    if not rows and t.total_ms is None:
        return None
    table = Table(title="Timing", show_header=False, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column(justify="right")
    for label, val in rows:
        table.add_row(label, f"{val:.0f} ms")
    if t.total_ms is not None:
        table.add_row("Total", f"[bold]{t.total_ms:.0f} ms[/bold]")
    return table


def _finding_panel(f: Finding) -> Panel:
    body = Text()
    body.append(f.explanation)
    body.append("\n")
    if f.evidence:
        body.append("\n")
        for k, v in f.evidence.items():
            body.append(f"  {k}: ", style="dim")
            body.append(f"{_evidence_value(v)}\n", style="dim")
    if f.suggested_fix:
        body.append("\n")
        body.append("Suggested fix: ", style="dim")
        body.append(f.suggested_fix)
    title = Text()
    title.append(f"[{_SEVERITY_LABEL[f.severity]}] ", style=_SEVERITY_STYLE[f.severity])
    title.append(f.title)
    return Panel(body, title=title, title_align="left", border_style=_SEVERITY_STYLE[f.severity])


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
