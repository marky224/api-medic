"""api-medic command-line interface.

The `api-medic` entry point lives at api_medic.cli.main:cli_entry
(see pyproject.toml). `api-medic <URL>` is shorthand for `api-medic run
<URL>` — the bare-URL form is handled by `_rewrite_bare_url` in
`cli_entry` before Typer parses, since putting a positional on the
parent group would swallow subcommand names like `serve`.

Subcommands:
  run        Execute an HTTP request and produce a Report.
  from-curl  Parse a curl command and (by default) execute it.
  from-har   Analyze a captured HAR file (no execution).
  serve      Launch the local web UI.
"""

from __future__ import annotations

import contextlib
import sys
from enum import Enum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated

import typer

from ..core.engine import analyze
from ..core.models import Report
from ..core.parser import parse_curl, parse_har
from ..core.render import render_html, render_json, render_markdown, render_terminal
from ..core.runner import run as runner_run

try:
    __version__ = _pkg_version("api-medic")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"


app = typer.Typer(
    name="api-medic",
    help="Diagnose HTTP API issues — run a request, parse a curl, analyze a HAR.",
    add_completion=False,
    no_args_is_help=True,
)

_SUBCOMMANDS = {"run", "from-curl", "from-har", "serve"}


def _rewrite_bare_url(argv: list[str]) -> list[str]:
    """Rewrite `<URL> [flags]` to `run <URL> [flags]` so the bare-URL form
    works without putting a positional on the parent callback (which would
    swallow subcommand names like `serve`).
    """
    if not argv:
        return argv
    first = argv[0]
    if first.startswith(("http://", "https://")) and first not in _SUBCOMMANDS:
        return ["run", *argv]
    return argv


def cli_entry() -> None:
    """Production entry point referenced by pyproject.toml's `api-medic` script."""
    # On Windows the default console encoding is cp1252, which can't encode
    # the arrow / em-dash glyphs the renderers use. Reconfigure to UTF-8 so
    # output round-trips on every platform the architecture commits to
    # (macOS, Linux, Windows).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")

    argv = _rewrite_bare_url(sys.argv[1:])
    app(argv)


class OutputFormat(str, Enum):
    terminal = "terminal"
    json = "json"
    markdown = "markdown"
    html = "html"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"api-medic {__version__}")
        raise typer.Exit()


# Shared option types -------------------------------------------------------

OutputOption = Annotated[
    OutputFormat,
    typer.Option("--output", "-o", help="Output format.", case_sensitive=False),
]

SaveOption = Annotated[
    Path | None,
    typer.Option("--save", help="Write the rendered report to a file instead of stdout."),
]

NoColorOption = Annotated[
    bool, typer.Option("--no-color", help="Disable ANSI colors in terminal output.")
]

VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Print a one-line findings summary to stderr.")
]


# Helpers -------------------------------------------------------------------


def _emit(
    report: Report,
    *,
    output: OutputFormat,
    save: Path | None,
    no_color: bool,
) -> None:
    """Render `report` and write it to stdout (or `save` if given)."""
    if output is OutputFormat.terminal:
        # Colour when stdout is a terminal AND user didn't suppress. When
        # writing to a file we still emit colour — modern terminals can `cat`
        # ANSI files happily — except when --no-color is set.
        color = not no_color and (save is not None or sys.stdout.isatty())
        text = render_terminal(report, color=color)
    elif output is OutputFormat.json:
        text = render_json(report)
    elif output is OutputFormat.markdown:
        text = render_markdown(report)
    elif output is OutputFormat.html:
        text = render_html(report)
    else:  # pragma: no cover — Enum exhausts above
        raise typer.BadParameter(f"Unknown output format: {output}")

    if save is not None:
        save.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {save}", err=True)
    else:
        typer.echo(text)


def _emit_findings_summary(report: Report) -> None:
    """One-line stderr summary, used by --verbose."""
    n = len(report.findings)
    if n == 0:
        typer.echo("No findings.", err=True)
        return
    critical = sum(1 for f in report.findings if f.severity == "critical")
    warning = sum(1 for f in report.findings if f.severity == "warning")
    info = sum(1 for f in report.findings if f.severity == "info")
    typer.echo(
        f"Findings: {n} total ({critical} critical, {warning} warning, {info} info)",
        err=True,
    )


def _parse_headers(items: list[str]) -> dict[str, str]:
    """Convert ['Name: value', ...] to {Name: value, ...}."""
    out: dict[str, str] = {}
    for raw in items:
        if ":" not in raw:
            raise typer.BadParameter(f"Header must be 'Name: value' — got {raw!r}.")
        name, value = raw.split(":", 1)
        out[name.strip()] = value.strip()
    return out


# Callback (top-level options only — bare-URL is handled by cli_entry) -----


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the api-medic version and exit.",
        ),
    ] = False,
) -> None:
    """api-medic — diagnose HTTP API issues.

    Pass a URL as the first argument as a shortcut for `run <URL>` —
    e.g. `api-medic https://api.example.com/health`.
    """
    pass


# Subcommands --------------------------------------------------------------


@app.command()
def run(
    url: Annotated[str, typer.Argument(help="URL to request.")],
    method: Annotated[str, typer.Option("--method", "-X", help="HTTP method.")] = "GET",
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header",
            "-H",
            help="Header in 'Name: value' form. Repeat for multiple.",
        ),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", "-d", help="Request body as a literal string."),
    ] = None,
    body_file: Annotated[
        Path | None,
        typer.Option("--body-file", help="Read the request body from a file."),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 30.0,
    output: OutputOption = OutputFormat.terminal,
    save: SaveOption = None,
    no_color: NoColorOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Execute an HTTP request and produce a diagnostic report."""
    if body is not None and body_file is not None:
        raise typer.BadParameter("--body and --body-file are mutually exclusive.")

    body_data: str | bytes | None = body_file.read_bytes() if body_file is not None else body

    headers = _parse_headers(header or [])

    captured = runner_run(method, url, headers=headers, body=body_data, timeout=timeout)
    report = analyze(captured)
    if verbose:
        _emit_findings_summary(report)
    _emit(report, output=output, save=save, no_color=no_color)


@app.command(name="from-curl")
def from_curl(
    curl: Annotated[str, typer.Argument(help="curl command string to parse.")],
    no_execute: Annotated[
        bool,
        typer.Option(
            "--no-execute",
            help=(
                "Skip executing the parsed request — analyze the request shape "
                "only. Useful when inspecting credentials you don't want to send."
            ),
        ),
    ] = False,
    output: OutputOption = OutputFormat.terminal,
    save: SaveOption = None,
    no_color: NoColorOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Parse a curl command and (by default) execute it."""
    captured = parse_curl(curl)
    if not no_execute:
        # Re-issue the parsed request through the live Runner so we get a real
        # response, timing, and DNS/TLS data.
        captured = runner_run(
            captured.method,
            captured.url,
            headers=captured.headers,
            body=captured.body if captured.body else None,
        )
    report = analyze(captured)
    if verbose:
        _emit_findings_summary(report)
    _emit(report, output=output, save=save, no_color=no_color)


@app.command(name="from-har")
def from_har(
    file: Annotated[Path, typer.Argument(help="Path to a .har file.")],
    output: OutputOption = OutputFormat.terminal,
    save: SaveOption = None,
    no_color: NoColorOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Analyze a captured HAR file (no execution)."""
    raw = file.read_text(encoding="utf-8")
    captured = parse_har(raw)
    report = analyze(captured)
    if verbose:
        _emit_findings_summary(report)
    _emit(report, output=output, save=save, no_color=no_color)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Host to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
) -> None:
    """Start the local web UI."""
    import uvicorn

    typer.echo(f"api-medic web UI on http://{host}:{port} (Ctrl+C to stop)", err=True)
    uvicorn.run("api_medic.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":  # pragma: no cover
    cli_entry()
