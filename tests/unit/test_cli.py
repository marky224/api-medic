"""Tests for cli.main using typer.testing.CliRunner.

The runner subcommand and bare-URL paths monkey-patch `runner_run` to
return a synthetic CapturedRequest so tests stay offline. One
@integration test at the bottom hits httpbin for the end-of-phase demo.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from api_medic import __version__
from api_medic.cli import main as cli_main
from api_medic.cli.main import _rewrite_bare_url, app
from api_medic.core import runner as core_runner
from api_medic.core.captured import CapturedDns, CapturedRequest, CapturedResponse
from api_medic.core.engine import analyze
from api_medic.core.models import TimingBreakdown
from api_medic.core.parser import parse_curl, parse_har
from api_medic.core.runner import validate_request
from api_medic.core.runner_safety import InvalidRequestError


def _captured(
    *,
    method: str = "GET",
    url: str = "https://api.example.com/v1/users",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    status: int = 401,
) -> CapturedRequest:
    return CapturedRequest(
        method=method,
        url=url,
        headers=headers or {},
        body=body,
        response=CapturedResponse(
            status_code=status,
            status_text="Unauthorized" if status == 401 else "OK",
            headers={"Content-Type": "application/json"},
            body=b'{"error":"invalid_token"}',
            protocol="HTTP/2",
        ),
        timing=TimingBreakdown(total_ms=120.0),
        source="live",
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_runner(monkeypatch):
    """Replace runner.run with a stub that records its call args."""
    calls: list[dict[str, Any]] = []

    def fake_run(method: str, url: str, **kwargs: Any) -> CapturedRequest:
        calls.append({"method": method, "url": url, **kwargs})
        return _captured(method=method.upper(), url=url, headers=kwargs.get("headers") or {})

    monkeypatch.setattr(cli_main, "runner_run", fake_run)
    return calls


# --- Basics ---------------------------------------------------------------


class TestVersionAndHelp:
    def test_version_prints_and_exits(self, runner):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip() == f"api-medic {__version__}"

    def test_help_listed_when_no_args(self, runner):
        # no_args_is_help=True prints help and exits 2 (Click convention for
        # "missing required input"). Help text lands on stderr in this mode.
        result = runner.invoke(app, [])
        assert result.exit_code in (0, 2)
        combined = result.stdout + (result.stderr or "")
        assert "Usage" in combined
        assert "run" in combined
        assert "from-curl" in combined
        assert "from-har" in combined
        assert "serve" in combined


# --- Bare URL & run -------------------------------------------------------


class TestBareUrlRewrite:
    """Bare-URL handling lives in cli_entry → _rewrite_bare_url, not in the
    Typer app itself (the parent group can't carry a positional without
    swallowing subcommand names). Tests cover the rewrite logic plus the
    end-to-end behaviour by invoking the rewritten args via CliRunner.
    """

    def test_rewrite_prepends_run_for_https_urls(self):
        assert _rewrite_bare_url(["https://api.example.com/v1/users"]) == [
            "run",
            "https://api.example.com/v1/users",
        ]

    def test_rewrite_preserves_flags_after_url(self):
        assert _rewrite_bare_url(["https://x.com/", "--output", "json", "--save", "out.json"]) == [
            "run",
            "https://x.com/",
            "--output",
            "json",
            "--save",
            "out.json",
        ]

    def test_rewrite_leaves_subcommands_alone(self):
        assert _rewrite_bare_url(["run", "https://x.com/"]) == ["run", "https://x.com/"]
        assert _rewrite_bare_url(["from-curl", "curl x"]) == ["from-curl", "curl x"]

    def test_rewrite_leaves_global_flags_alone(self):
        assert _rewrite_bare_url(["--version"]) == ["--version"]
        assert _rewrite_bare_url(["--help"]) == ["--help"]

    def test_rewrite_leaves_empty_argv(self):
        assert _rewrite_bare_url([]) == []

    def test_bare_url_end_to_end(self, runner, fake_runner):
        rewritten = _rewrite_bare_url(["https://api.example.com/v1/users"])
        result = runner.invoke(app, rewritten)
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        assert fake_runner[0]["method"] == "GET"
        assert fake_runner[0]["url"] == "https://api.example.com/v1/users"

    def test_bare_url_with_json_output(self, runner, fake_runner):
        rewritten = _rewrite_bare_url(["https://api.example.com/v1/users", "--output", "json"])
        result = runner.invoke(app, rewritten)
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["request"]["method"] == "GET"
        assert body["response"]["status_code"] == 401

    def test_bare_url_with_save(self, runner, fake_runner, tmp_path):
        out = tmp_path / "report.json"
        rewritten = _rewrite_bare_url(
            [
                "https://api.example.com/v1/users",
                "--output",
                "json",
                "--save",
                str(out),
            ]
        )
        result = runner.invoke(app, rewritten)
        assert result.exit_code == 0
        body = json.loads(out.read_text(encoding="utf-8"))
        assert body["request"]["method"] == "GET"


class TestRunCommand:
    def test_run_with_method_and_headers(self, runner, fake_runner):
        result = runner.invoke(
            app,
            [
                "run",
                "https://api.example.com/v1/users",
                "-X",
                "POST",
                "-H",
                "Authorization: Bearer xyz",
                "-H",
                "Content-Type: application/json",
                "--body",
                '{"name":"alex"}',
            ],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        assert len(fake_runner) == 1
        call = fake_runner[0]
        assert call["method"] == "POST"
        assert call["headers"] == {
            "Authorization": "Bearer xyz",
            "Content-Type": "application/json",
        }
        assert call["body"] == '{"name":"alex"}'

    def test_run_body_file_reads_path(self, runner, fake_runner, tmp_path):
        body_path = tmp_path / "req.json"
        body_path.write_bytes(b'{"x":1}')
        result = runner.invoke(
            app,
            [
                "run",
                "https://api.example.com/x",
                "-X",
                "POST",
                "--body-file",
                str(body_path),
            ],
        )
        assert result.exit_code == 0
        assert fake_runner[0]["body"] == b'{"x":1}'

    def test_run_body_and_body_file_mutually_exclusive(self, runner, fake_runner, tmp_path):
        body_path = tmp_path / "req.json"
        body_path.write_text("{}")
        result = runner.invoke(
            app,
            [
                "run",
                "https://api.example.com/x",
                "--body",
                "{}",
                "--body-file",
                str(body_path),
            ],
        )
        assert result.exit_code != 0
        # BadParameter messages land on stderr.
        assert "mutually exclusive" in (result.stderr or result.stdout)

    def test_malformed_header_is_rejected(self, runner, fake_runner):
        result = runner.invoke(app, ["run", "https://x.com/", "-H", "no-colon-here"])
        assert result.exit_code != 0
        assert "Header must be" in (result.stderr or result.stdout)


# --- Unencodable text ------------------------------------------------------


def _jwt(payload: bytes) -> str:
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{b64}.sig"


# Payload {"sub":"\ud800","exp":0} as written: JSON's escape for an
# unpaired surrogate. The expired-token finding copies sub into evidence.
_SURROGATE_SUB_JWT = _jwt(b'{"sub":"\\ud800","exp":0}')


class TestUnencodableText:
    """Input can carry an unpaired surrogate (a HAR's "\\ud800" escape, or
    an argument that isn't valid UTF-8, which Python decodes to one). UTF-8
    can't encode it, so -o json can't, and the other formats can't write
    text that shows it: the CLI says so and exits 2, as it does for other
    bad input, instead of printing a traceback."""

    _MESSAGE = "input holds text that can't be encoded as JSON"

    def test_from_curl(self, runner: CliRunner) -> None:
        curl = "curl https://api.example.com/ -H 'X-Note: a\ud800b'"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute", "-o", "json"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert self._MESSAGE in result.stderr
        assert result.stdout == ""

    def test_from_har(self, runner: CliRunner, tmp_path: Any) -> None:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.example.com/",
                            "headers": [{"name": "X-Note", "value": "a\ud800b"}],
                        }
                    }
                ]
            }
        }
        path = tmp_path / "in.har"
        path.write_text(json.dumps(har), encoding="utf-8")
        result = runner.invoke(app, ["from-har", str(path), "-o", "json"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert self._MESSAGE in result.stderr

    @pytest.mark.parametrize("fmt", ["json", "terminal", "markdown", "html"])
    def test_surrogate_the_output_shows(self, runner: CliRunner, fmt: str) -> None:
        # The expired-token finding shows the JWT's sub in every format.
        curl = f"curl https://api.example.com/ -H 'Authorization: Bearer {_SURROGATE_SUB_JWT}'"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute", "-o", fmt])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert self._MESSAGE in result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize("fmt", ["terminal", "markdown", "html"])
    def test_save_writes_nothing(self, runner: CliRunner, tmp_path: Any, fmt: str) -> None:
        curl = f"curl https://api.example.com/ -H 'Authorization: Bearer {_SURROGATE_SUB_JWT}'"
        path = tmp_path / "report.out"
        result = runner.invoke(
            app, ["from-curl", curl, "--no-execute", "-o", fmt, "--save", str(path)]
        )
        assert result.exit_code == 2
        assert self._MESSAGE in result.stderr
        assert not path.exists()

    @pytest.mark.parametrize("fmt", ["terminal", "markdown", "html"])
    def test_surrogate_the_output_does_not_show(self, runner: CliRunner, fmt: str) -> None:
        # These formats don't show request headers, so the text encodes.
        curl = "curl https://api.example.com/ -H 'X-Note: a\ud800b'"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute", "-o", fmt])
        assert result.exit_code == 0, result.stderr
        assert "api.example.com" in result.stdout

    @pytest.mark.parametrize(
        "entry",
        [
            {
                "request": {
                    "method": "POST",
                    "url": "https://api.example.com/",
                    "postData": {"text": "a\ud800"},
                }
            },
            {
                "request": {"method": "GET", "url": "https://api.example.com/"},
                "response": {"status": 200, "content": {"text": "\ud800"}},
            },
        ],
        ids=["post-data", "response-content"],
    )
    def test_from_har_body_text(self, runner: CliRunner, tmp_path: Any, entry: Any) -> None:
        path = tmp_path / "in.har"
        path.write_text(json.dumps({"log": {"entries": [entry]}}), encoding="utf-8")
        result = runner.invoke(app, ["from-har", str(path)])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert self._MESSAGE in result.stderr

    def test_from_curl_data(self, runner: CliRunner) -> None:
        curl = "curl https://api.example.com/ -d 'a\ud800'"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert self._MESSAGE in result.stderr


# Remaining with 309+ more digits than Limit: remaining / limit is too large
# for a float and raised OverflowError, which nothing caught.
_HUGE_REMAINING = ["1" + "0" * 400, "9" * 4300]
_HUGE_REMAINING_IDS = ["1e400", "4300-nines"]


def _rate_limited_har(remaining: str) -> dict[str, Any]:
    headers = [
        {"name": "X-RateLimit-Limit", "value": "1"},
        {"name": "X-RateLimit-Remaining", "value": remaining},
    ]
    entry = {
        "request": {"method": "GET", "url": "https://api.example.com/"},
        "response": {"status": 200, "headers": headers},
    }
    return {"log": {"entries": [entry]}}


@pytest.mark.parametrize("remaining", _HUGE_REMAINING, ids=_HUGE_REMAINING_IDS)
def test_from_har_approaching_ratio_too_large_for_a_float(
    runner: CliRunner, tmp_path: Any, remaining: str
) -> None:
    """This used to print an OverflowError traceback and exit 1."""
    findings = []
    for value in (remaining, "1"):
        path = tmp_path / "in.har"
        path.write_text(json.dumps(_rate_limited_har(value)), encoding="utf-8")
        result = runner.invoke(app, ["from-har", str(path), "-o", "json"])
        assert result.exit_code == 0, result.stderr
        findings.append(json.loads(result.stdout)["findings"])
    assert findings[0] == findings[1]


# --- Input analyze() can't make a Report of --------------------------------


def _flat(text: str) -> str:
    """`text` without Rich's colour codes, error box and line wrapping.

    Rich forces colour when GITHUB_ACTIONS is set, so CI output carries ANSI codes.
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[\u2500-\u257f]", " ", text).split())


class TestInputThatCantMakeAReport:
    """A URL or method holding an unpaired surrogate parses, but analyze()
    raises ValueError: RequestSummary can't validate it. from-har and
    from-curl answer it as bad input, exit 2 with the text the Lambda's and
    serve's 400 carry. They used to print a traceback and exit 1."""

    @pytest.mark.parametrize(
        "request_",
        [
            {"method": "GET", "url": "https://x.com/\ud800"},
            {"method": "G\ud800T", "url": "https://x.com/"},
        ],
        ids=["url", "method"],
    )
    def test_from_har(self, runner: CliRunner, tmp_path: Any, request_: dict[str, Any]) -> None:
        har = {"log": {"entries": [{"request": request_}]}}
        with pytest.raises(ValueError) as e:
            analyze(parse_har(har))
        path = tmp_path / "in.har"
        path.write_text(json.dumps(har), encoding="utf-8")
        result = runner.invoke(app, ["from-har", str(path)])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)
        assert result.stdout == ""

    @pytest.mark.parametrize("execute", [False, True], ids=["no-execute", "live"])
    def test_from_curl(self, runner: CliRunner, fake_runner: Any, execute: bool) -> None:
        # The fake runner returns the URL as given, so analyze() gets it live too.
        curl = "curl 'https://x.com/\ud800'"
        with pytest.raises(ValueError) as e:
            analyze(parse_curl(curl))
        args = ["from-curl", curl] if execute else ["from-curl", curl, "--no-execute"]
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)
        assert len(fake_runner) == (1 if execute else 0)

    @pytest.mark.parametrize(
        "url", ["https://x.com/\ud800", "https://x.com:99999/"], ids=["surrogate", "bad-port"]
    )
    def test_from_curl_live_url_the_runner_rejects(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        # The real runner refuses these before any lookup or send, with the
        # message the Lambda's and serve's /api/run 400 carry.
        def no_network(*a: Any, **kw: Any) -> Any:
            raise AssertionError("looked up or probed a host")

        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(core_runner, "_resolve_dns", no_network)
        monkeypatch.setattr(core_runner, "_probe_tls", no_network)
        with pytest.raises(InvalidRequestError) as e:
            validate_request("GET", url, {}, None)
        result = runner.invoke(app, ["from-curl", f"curl '{url}'"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)

    def test_from_curl_live_method_that_isnt_ascii_is_refused(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A long s (U+017F): changing the method's case made it POST, which
        # was sent. The real runner refuses the method as given.
        def no_network(*a: Any, **kw: Any) -> Any:
            raise AssertionError("looked up or probed a host")

        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(core_runner, "_resolve_dns", no_network)
        monkeypatch.setattr(core_runner, "_probe_tls", no_network)
        with pytest.raises(InvalidRequestError) as e:
            validate_request("PO\u017fT", "https://api.example.com/", {}, None)
        result = runner.invoke(app, ["from-curl", "curl -X PO\u017fT https://api.example.com/"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)

    @pytest.mark.parametrize(
        "url", ["https://x.com/\ud800", "https://x.com:99999/"], ids=["surrogate", "bad-port"]
    )
    @pytest.mark.parametrize("bare", [False, True], ids=["run", "bare-url"])
    def test_run_url_the_runner_rejects(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, url: str, bare: bool
    ) -> None:
        # These used to print an InvalidURLError traceback and exit 1.
        def no_network(*a: Any, **kw: Any) -> Any:
            raise AssertionError("looked up or probed a host")

        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(core_runner, "_resolve_dns", no_network)
        monkeypatch.setattr(core_runner, "_probe_tls", no_network)
        with pytest.raises(InvalidRequestError) as e:
            validate_request("GET", url, {}, None)
        result = runner.invoke(app, _rewrite_bare_url([url]) if bare else ["run", url])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)

    def test_run_method_h11_cant_send(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # h11, httpx's HTTP/1.1 writer, refused it once the connection was
        # open: the report had no response and the run exited 0.
        def no_network(*a: Any, **kw: Any) -> Any:
            raise AssertionError("looked up or probed a host")

        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(core_runner, "_resolve_dns", no_network)
        monkeypatch.setattr(core_runner, "_probe_tls", no_network)
        with pytest.raises(InvalidRequestError) as e:
            validate_request("GE T", "https://api.example.com/", {}, None)
        result = runner.invoke(app, ["run", "https://api.example.com/", "-X", "GE T"])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert _flat(str(e.value)) in _flat(result.stderr)
        assert "Method can only hold letters, digits and" in _flat(result.stderr)


# JSON-typed HAR bodies, which reach the check as UTF-8, and the parse error
# body.malformed_json reports for each (None: no finding). NUL bytes used to
# make json.loads read the first three as UTF-16, and int() stopped the
# fourth's parse before the "x".
_NUL_PARSE_ERROR = "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
_HAR_JSON_BODIES = [
    ("{\x00}\x00", _NUL_PARSE_ERROR),
    ("\x00{\x00}", "Expecting value: line 1 column 1 (char 0)"),
    ("{\x00}\x00 ", _NUL_PARSE_ERROR),
    ("9" * 4301 + "x", "Extra data: line 1 column 4302 (char 4301)"),
    ("9" * 4301, None),
    ("[" * 100_000 + "]" * 100_000, None),
    ("[" * 100_000, None),
]
_HAR_JSON_BODIES_IDS = [
    "utf-16-le-nul",
    "utf-16-be-nul",
    "odd-length-nul",
    "long-int-then-junk",
    "long-int",
    "nested-100000-deep",
    "nested-100000-deep-malformed",
]


def _json_body_har(text: str) -> dict[str, Any]:
    response = {
        "status": 200,
        "headers": [{"name": "Content-Type", "value": "application/json"}],
        "content": {"text": text},
    }
    entry = {"request": {"method": "GET", "url": "https://api.example.com/"}, "response": response}
    return {"log": {"entries": [entry]}}


def _parse_errors(findings: list[dict[str, Any]]) -> list[str]:
    return [f["evidence"]["parse_error"] for f in findings if f["id"] == "body.malformed_json"]


@pytest.mark.parametrize(("text", "parse_error"), _HAR_JSON_BODIES, ids=_HAR_JSON_BODIES_IDS)
def test_from_har_json_body_is_read_as_utf_8(
    runner: CliRunner, tmp_path: Any, text: str, parse_error: str | None
) -> None:
    path = tmp_path / "in.har"
    path.write_text(json.dumps(_json_body_har(text)), encoding="utf-8")
    result = runner.invoke(app, ["from-har", str(path), "-o", "json"])
    assert result.exit_code == 0, result.stderr
    findings = json.loads(result.stdout)["findings"]
    assert _parse_errors(findings) == ([] if parse_error is None else [parse_error])


# --- Stream-encoding regression -------------------------------------------


class TestForceUtf8Streams:
    """Windows defaults stdout to cp1252, which can't encode the arrow / em-dash
    glyphs the renderers emit. The CLI must reconfigure to UTF-8 regardless of
    which entry point launched it (cli_entry vs. typer app() directly), since
    pip-generated `.exe` launchers built before `cli_entry` existed call `app()`
    directly. Verify reconfigure runs both via the helper and via the callback.
    """

    def test_helper_reconfigures_streams_with_reconfigure_method(self, monkeypatch):

        captured: list[tuple[str, str]] = []

        class FakeStream:
            encoding = "cp1252"

            def reconfigure(self, *, encoding: str) -> None:
                captured.append((id(self).__class__.__name__, encoding))
                self.encoding = encoding

        fake_out, fake_err = FakeStream(), FakeStream()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)
        cli_main._force_utf8_streams()
        assert fake_out.encoding == "utf-8"
        assert fake_err.encoding == "utf-8"

    def test_helper_skips_streams_without_reconfigure(self, monkeypatch):
        # Streams that don't expose .reconfigure (BytesIO, custom test wrappers)
        # must be skipped without raising — common in test runners.
        import io

        monkeypatch.setattr(sys, "stdout", io.BytesIO())
        monkeypatch.setattr(sys, "stderr", io.BytesIO())
        cli_main._force_utf8_streams()  # must not raise

    def test_callback_invokes_force_utf8(self, runner, fake_runner, monkeypatch):
        # Even when `app()` is called directly (stale .exe entry point that
        # bypasses `cli_entry`), the callback runs before any subcommand and
        # must reconfigure the streams.
        called: list[bool] = []
        monkeypatch.setattr(cli_main, "_force_utf8_streams", lambda: called.append(True))
        result = runner.invoke(app, ["run", "https://api.example.com/v1/users", "--output", "json"])
        assert result.exit_code == 0
        assert called, "callback did not invoke _force_utf8_streams"


# --- Renderer-emission regression -----------------------------------------


class TestNoDuplicateRender:
    """`render_terminal` historically attached its rich Console to sys.stdout
    while also recording into an export buffer; the CLI then echoed the
    buffered text, which surfaced the report twice on a single invocation.
    The Console is now backed by an in-memory file — verify only one emission
    reaches stdout for the default terminal output and for --save."""

    def test_request_line_appears_once_in_terminal_output(self, runner, fake_runner):
        result = runner.invoke(
            app,
            ["run", "https://api.example.com/v1/users", "--no-color"],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        # The request line is emitted by the terminal renderer for every
        # rendered Report. If the renderer were double-emitting (printing to
        # stdout AND returning the recorded text to the CLI for typer.echo),
        # this would be 2 — the symptom users reported as Bug 2.
        assert result.stdout.count("https://api.example.com/v1/users") == 1

    def test_save_does_not_also_emit_to_stdout(self, runner, fake_runner, tmp_path):
        out = tmp_path / "report.txt"
        result = runner.invoke(
            app,
            [
                "run",
                "https://api.example.com/v1/users",
                "--no-color",
                "--save",
                str(out),
            ],
        )
        assert result.exit_code == 0
        # With --save, the report goes to the file; stdout should not contain
        # the rendered report. The "Wrote ..." message lands on stderr.
        assert "https://api.example.com/v1/users" not in result.stdout
        assert "https://api.example.com/v1/users" in out.read_text(encoding="utf-8")


# --- Verbose --------------------------------------------------------------


class TestVerbose:
    def test_verbose_prints_findings_summary_to_stderr(self, runner, fake_runner):
        result = runner.invoke(
            app,
            ["run", "https://api.example.com/v1/users", "-v", "--output", "json"],
        )
        assert result.exit_code == 0
        assert "Findings:" in (result.stderr or "")
        body = json.loads(result.stdout)
        assert body["request"]["url"].endswith("/v1/users")


# --- from-curl ------------------------------------------------------------


class TestFromCurl:
    def test_executes_by_default(self, runner, fake_runner):
        result = runner.invoke(
            app,
            [
                "from-curl",
                "curl -X POST 'https://api.example.com/v1/users' -H 'Content-Type: application/json'",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        # The runner was called → execute-by-default behaviour.
        assert len(fake_runner) == 1
        assert fake_runner[0]["method"] == "POST"
        assert fake_runner[0]["url"] == "https://api.example.com/v1/users"

    def test_no_execute_skips_runner(self, runner, fake_runner):
        result = runner.invoke(
            app,
            [
                "from-curl",
                "curl -X POST 'https://api.example.com/v1/users'",
                "--no-execute",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0
        assert fake_runner == []  # never called
        body = json.loads(result.stdout)
        assert body["source"] == "curl"
        assert body["response"] is None  # parser-only — no response

    def test_no_execute_shows_the_method_as_given(self, runner, fake_runner):
        curl = "curl -X put https://api.example.com/"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute", "--output", "json"])
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["request"]["method"] == "put"

    def test_live_run_sends_the_cookie_header(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[httpx.Headers] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request.headers)
            return httpx.Response(200)

        def run_offline(*args: Any, **kwargs: Any) -> CapturedRequest:
            return core_runner.run(*args, **kwargs, transport=httpx.MockTransport(respond))

        def resolve_dns(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
            return CapturedDns(records=["93.184.216.34"]), 0.0

        def probe_tls(host: str, port: int, *a: Any, **kw: Any) -> tuple[None, float]:
            return None, 0.0

        monkeypatch.setattr(core_runner, "_resolve_dns", resolve_dns)
        monkeypatch.setattr(core_runner, "_probe_tls", probe_tls)
        monkeypatch.setattr(cli_main, "runner_run", run_offline)
        curl = "curl https://api.example.com/ -H 'Cookie: b=2; a=1' -H 'Cookie: c=3'"
        result = runner.invoke(app, ["from-curl", curl, "-o", "json"])
        assert result.exit_code == 0, result.stderr
        assert [h.get_list("Cookie") for h in sent] == [["b=2; a=1; c=3"]]
        assert json.loads(result.stdout)["request"]["headers"]["Cookie"] == "b=2; a=1; c=3"

    def test_live_run_sends_the_cookie_option_as_the_cookie_header(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # uncurl reads -b as --data-binary: from-curl sent a POST whose body
        # was the cookie.
        sent: list[tuple[str, list[str], bytes]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append((request.method, request.headers.get_list("Cookie"), request.content))
            return httpx.Response(200)

        def run_offline(*args: Any, **kwargs: Any) -> CapturedRequest:
            return core_runner.run(*args, **kwargs, transport=httpx.MockTransport(respond))

        def resolve_dns(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
            return CapturedDns(records=["93.184.216.34"]), 0.0

        def probe_tls(host: str, port: int, *a: Any, **kw: Any) -> tuple[None, float]:
            return None, 0.0

        monkeypatch.setattr(core_runner, "_resolve_dns", resolve_dns)
        monkeypatch.setattr(core_runner, "_probe_tls", probe_tls)
        monkeypatch.setattr(cli_main, "runner_run", run_offline)
        curl = "curl https://api.example.com/ -b 'b=2; a=1'"
        result = runner.invoke(app, ["from-curl", curl, "-o", "json"])
        assert result.exit_code == 0, result.stderr
        assert sent == [("GET", ["b=2; a=1"], b"")]

    @pytest.mark.parametrize(
        ("between", "message"),
        [
            ("\\n", "Header values can't hold '\\n' (header 'Cookie')."),
            ("\\u00a0", "Header names and values must be ASCII (header 'Cookie')."),
        ],
        ids=["line-break", "non-breaking-space"],
    )
    def test_live_run_refuses_whitespace_a_header_cant_carry_between_cookie_options(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, between: str, message: str
    ) -> None:
        # curl joins -b values as given, so the Cookie header holds what is
        # between them. A line break was sent to h11, which refused it once
        # the connection was open: no response, and exit 0.
        sent: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200)

        def run_offline(*args: Any, **kwargs: Any) -> CapturedRequest:
            return core_runner.run(*args, **kwargs, transport=httpx.MockTransport(respond))

        def resolve_dns(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
            return CapturedDns(records=["93.184.216.34"]), 0.0

        def probe_tls(host: str, port: int, *a: Any, **kw: Any) -> tuple[None, float]:
            return None, 0.0

        monkeypatch.setattr(core_runner, "_resolve_dns", resolve_dns)
        monkeypatch.setattr(core_runner, "_probe_tls", probe_tls)
        monkeypatch.setattr(cli_main, "runner_run", run_offline)
        curl = f"curl https://api.example.com/ -b $'a=1{between}' -b 'b=2'"
        result = runner.invoke(app, ["from-curl", curl])
        assert result.exit_code == 2, (result.exit_code, result.stdout)
        assert isinstance(result.exception, SystemExit)
        assert _flat(message) in _flat(result.stderr)
        assert sent == []

    @pytest.mark.parametrize(
        "curl",
        [
            # One character over parser.MAX_CURL_CHARS (50,000).
            "curl https://api.example.com/ -d '" + "a" * 49_966 + "'",
            # One argument over parser.MAX_CURL_ARGS (1,000).
            "curl https://api.example.com/" + " -k" * 999,
            # A Cookie value one character over parser.MAX_CURL_COOKIE_CHARS (4,096).
            "curl https://api.example.com/ -H 'Cookie:" + "=" * 4_096 + "\U0001f600'",
            "   ",
            "definitely not a curl command",
            # A -b file to read cookies from, and a -c file to write them to.
            "curl https://api.example.com/ -b cookies.txt",
            "curl https://api.example.com/ -c jar.txt",
            # A -b value one character over parser.MAX_CURL_COOKIE_CHARS.
            "curl https://api.example.com/ -b '" + "=" * 4_096 + "\U0001f600'",
        ],
        ids=[
            "over-the-limit",
            "too-many-args",
            "cookie-too-long",
            "empty",
            "unparseable",
            "cookie-file",
            "cookie-jar",
            "cookie-option-too-long",
        ],
    )
    def test_command_parse_curl_rejects_exits_2(self, runner, fake_runner, curl):
        # The empty and unparseable ones used to print a traceback and exit 1.
        result = runner.invoke(app, ["from-curl", curl])
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert _flat(str(e.value)) in _flat(result.stderr)
        assert result.stdout == ""
        assert fake_runner == []


# --- from-har -------------------------------------------------------------


class TestFromHar:
    def test_har_file_parsed(self, runner, fake_runner, tmp_path):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://api.example.com/v1/users",
                            "headers": [{"name": "Authorization", "value": "Bearer x"}],
                        },
                        "response": {
                            "status": 401,
                            "statusText": "Unauthorized",
                            "headers": [],
                            "httpVersion": "HTTP/2",
                        },
                        "timings": {},
                    }
                ],
            }
        }
        path = tmp_path / "session.har"
        path.write_text(json.dumps(har), encoding="utf-8")

        result = runner.invoke(app, ["from-har", str(path), "--output", "json"])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        # No execution for HAR.
        assert fake_runner == []
        body = json.loads(result.stdout)
        assert body["source"] == "har"
        assert body["request"]["method"] == "POST"
        assert body["response"]["status_code"] == 401


# --- Files the CLI can't read or write --------------------------------------


def _os_reason(action: Callable[[], object]) -> str:
    """The strerror of the OSError `action` raises."""
    try:
        action()
    except OSError as e:
        return str(e.strerror)
    pytest.skip("this platform or user can read or write it")


def _unspaced(text: str) -> str:
    """_flat(text) without any whitespace: Rich folds a long path anywhere."""
    return "".join(_flat(text).split())


def _assert_cant(result: Any, action: str, path: Path, reason: str) -> None:
    assert result.exit_code == 2, (result.exit_code, repr(result.exception))
    assert isinstance(result.exception, SystemExit)
    assert _unspaced(f"Can't {action} {path}: {reason}.") in _unspaced(result.stderr)
    assert result.stdout == ""


def _unreadable(path: Path) -> Path:
    path.write_text("{}", encoding="utf-8")
    path.chmod(0)
    return path


class TestFilesTheCliCantRead:
    """A path that doesn't exist or a directory printed a FileNotFoundError
    or IsADirectoryError traceback and exited 1."""

    @pytest.mark.parametrize("kind", ["missing", "directory"])
    def test_from_har(self, runner: CliRunner, tmp_path: Path, kind: str) -> None:
        path = tmp_path / "missing.har" if kind == "missing" else tmp_path
        reason = _os_reason(lambda: path.read_text(encoding="utf-8"))
        _assert_cant(runner.invoke(app, ["from-har", str(path)]), "read", path, reason)

    @pytest.mark.parametrize("kind", ["missing", "directory"])
    def test_run_body_file(
        self, runner: CliRunner, fake_runner: Any, tmp_path: Path, kind: str
    ) -> None:
        path = tmp_path / "missing.json" if kind == "missing" else tmp_path
        reason = _os_reason(path.read_bytes)
        result = runner.invoke(app, ["run", "https://api.example.com/", "--body-file", str(path)])
        _assert_cant(result, "read", path, reason)
        assert fake_runner == []

    @pytest.mark.parametrize("command", ["from-har", "run"])
    def test_file_without_read_permission(
        self, runner: CliRunner, fake_runner: Any, tmp_path: Path, command: str
    ) -> None:
        # Typer's Path type checks this before the command runs.
        path = _unreadable(tmp_path / "in.har")
        _os_reason(lambda: path.read_text(encoding="utf-8"))
        args = (
            ["from-har", str(path)]
            if command == "from-har"
            else ["run", "https://api.example.com/", "--body-file", str(path)]
        )
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        # Typer 0.29 lowercases "Path" in "Path '...' is not readable.",
        # so match the part 0.28 and 0.29 both print.
        assert _unspaced(f"'{path}' is not readable.") in _unspaced(result.stderr)
        assert fake_runner == []


class TestSavePathTheCliCantWrite:
    """--save to a path in a directory that doesn't exist, to a directory
    or into a directory without write permission printed a
    FileNotFoundError, IsADirectoryError or PermissionError traceback and
    exited 1, for every command that takes it."""

    @pytest.mark.parametrize("kind", ["missing-directory", "directory", "unwritable"])
    @pytest.mark.parametrize("command", ["run", "from-curl", "from-har"])
    def test_save(
        self, runner: CliRunner, fake_runner: Any, tmp_path: Path, kind: str, command: str
    ) -> None:
        har = tmp_path / "in.har"
        har.write_text(
            json.dumps({"log": {"entries": [{"request": {"method": "GET", "url": "https://x/"}}]}}),
            encoding="utf-8",
        )
        args = {
            "run": ["run", "https://api.example.com/"],
            "from-curl": ["from-curl", "curl https://api.example.com/", "--no-execute"],
            "from-har": ["from-har", str(har)],
        }[command]
        if kind == "unwritable":
            out_dir = tmp_path / "read-only"
            out_dir.mkdir()
            out_dir.chmod(0o500)
        path = {
            "missing-directory": tmp_path / "no-such-directory" / "report.txt",
            "directory": tmp_path,
            "unwritable": tmp_path / "read-only" / "report.txt",
        }[kind]
        reason = _os_reason(lambda: path.write_text("x", encoding="utf-8"))
        result = runner.invoke(app, [*args, "--save", str(path)])
        _assert_cant(result, "write", path, reason)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="macOS refuses a file name that isn't UTF-8"
)
class TestFileNameThatIsNotUtf8:
    """A name that isn't valid UTF-8 reaches Python holding a surrogate
    ("\\udcff" for byte 0xff), which stderr can't print. Messages naming
    it show it escaped."""

    def test_missing_file(self, runner: CliRunner, tmp_path: Path) -> None:
        path = tmp_path / "\udcff.har"
        result = runner.invoke(app, ["from-har", str(path)])
        _assert_cant(result, "read", tmp_path / "\\udcff.har", "No such file or directory")

    def test_save_says_where_it_wrote(self, runner: CliRunner, tmp_path: Path) -> None:
        # The report was written, then printing "Wrote <path>" raised
        # UnicodeEncodeError and exited 1.
        path = tmp_path / "\udcff.json"
        args = ["from-curl", "curl https://api.example.com/", "--no-execute", "-o", "json"]
        result = runner.invoke(app, [*args, "--save", str(path)])
        assert result.exit_code == 0, repr(result.exception)
        assert json.loads(path.read_text(encoding="utf-8"))["source"] == "curl"
        assert _unspaced(f"Wrote {tmp_path}/\\udcff.json") in _unspaced(result.stderr)


# --- Control characters from the input --------------------------------------

_C1_URL = "https://api.example.com/\x1bc\x9b2J"
_C1_URL_SHOWN = "https://api.example.com/\\x1bc\\x9b2J"
# A control character api-medic didn't write: C0 but tab and line feed, DEL
# and C1.
_RAW_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# The colour codes Rich writes in Typer's error box when CI sets GITHUB_ACTIONS.
_RICH_STYLE = re.compile(r"\x1b\[[0-9;]*m")


def _raw_controls(text: str) -> list[str]:
    """The control characters _RAW_CONTROL finds in `text`, in hex."""
    return sorted({f"0x{ord(c):02x}" for c in _RAW_CONTROL.findall(text)})


def _har_path(
    tmp_path: Path, request: dict[str, Any], response: dict[str, Any] | None = None
) -> Path:
    entry: dict[str, Any] = {"request": request}
    if response is not None:
        entry["response"] = response
    path = tmp_path / "in.har"
    path.write_text(json.dumps({"log": {"entries": [entry]}}), encoding="utf-8")
    return path


class TestControlCharactersFromTheInput:
    """A terminal reads ESC and C1 characters as commands: clear the screen,
    move the cursor, retitle the window, make a link. The CLI printed the
    ones in its input as they were. It shows them as Python writes them in
    a string. CliRunner runs with color=True: without it, Click drops
    ESC [ ... sequences from what the CLI writes, as it does in a pipe."""

    @pytest.mark.parametrize("fmt", ["terminal", "markdown", "html", "json"])
    def test_report_shows_them_escaped(self, runner: CliRunner, tmp_path: Path, fmt: str) -> None:
        path = _har_path(
            tmp_path,
            {"method": "GET", "url": _C1_URL},
            {
                "status": 200,
                "statusText": "O\x9bK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "content": {"text": "{\x1bc\x7f"},
            },
        )
        result = runner.invoke(app, ["from-har", str(path), "-o", fmt], color=True)
        assert result.exit_code == 0, result.stderr
        assert _raw_controls(result.stdout) == []
        if fmt == "json":
            # JSON escapes them, so the report still holds the URL as given.
            assert json.loads(result.stdout)["request"]["url"] == _C1_URL
        else:
            assert _C1_URL_SHOWN in result.stdout

    def test_from_har_error_shows_them_escaped(self, runner: CliRunner, tmp_path: Path) -> None:
        # An entry with no method: parse_har's message quotes its URL.
        path = _har_path(tmp_path, {"url": _C1_URL})
        result = runner.invoke(app, ["from-har", str(path)], color=True)
        assert result.exit_code == 2
        assert _raw_controls(_RICH_STYLE.sub("", result.stderr)) == []
        shown = f"HAR entry[0] ({_C1_URL_SHOWN}): request.method is missing."
        assert _unspaced(shown) in _unspaced(result.stderr)

    def test_request_line_shows_them_escaped(self, runner: CliRunner) -> None:
        curl = f"curl -X 'P\x1bcUT' '{_C1_URL}'"
        result = runner.invoke(app, ["from-curl", curl, "--no-execute"], color=True)
        assert result.exit_code == 0, result.stderr
        assert _raw_controls(result.stdout) == []
        assert f"P\\x1bcUT {_C1_URL_SHOWN}" in result.stdout

    def test_file_name_shows_them_escaped(self, runner: CliRunner, tmp_path: Path) -> None:
        path = tmp_path / "\x1bc.har"
        reason = _os_reason(lambda: path.read_text(encoding="utf-8"))
        result = runner.invoke(app, ["from-har", str(path)], color=True)
        assert result.exit_code == 2
        assert _raw_controls(_RICH_STYLE.sub("", result.stderr)) == []
        shown = str(path).replace("\x1b", "\\x1b")
        assert _unspaced(f"Can't read {shown}: {reason}.") in _unspaced(result.stderr)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Windows refuses a control character in a file name"
    )
    def test_save_says_where_it_wrote_with_them_escaped(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        path = tmp_path / "\x1bc.json"
        args = ["from-curl", "curl https://api.example.com/", "--no-execute", "-o", "json"]
        result = runner.invoke(app, [*args, "--save", str(path)], color=True)
        assert result.exit_code == 0, repr(result.exception)
        assert json.loads(path.read_text(encoding="utf-8"))["source"] == "curl"
        assert _raw_controls(result.stderr) == []
        assert _unspaced(f"Wrote {tmp_path}/\\x1bc.json") in _unspaced(result.stderr)

    @pytest.mark.parametrize(
        "protocol",
        ["[link=https://evil.example/]HTTP/1.1[/link]", "[/bold]"],
        ids=["link", "unmatched-close"],
    )
    def test_protocol_is_shown_as_text(
        self, runner: CliRunner, tmp_path: Path, protocol: str
    ) -> None:
        # Rich read a HAR's httpVersion as markup: the first made a link,
        # and the second raised MarkupError, a traceback and exit 1.
        response = {"status": 200, "headers": [], "httpVersion": protocol}
        path = _har_path(tmp_path, {"method": "GET", "url": "https://api.example.com/"}, response)
        result = runner.invoke(app, ["from-har", str(path)], color=True)
        assert result.exit_code == 0, repr(result.exception)
        assert protocol in result.stdout

    def test_escaped_copy_escapes_keys_too(self, tmp_path: Path) -> None:
        """No output prints a key from the input today: the renderers print
        a finding's evidence, whose keys api-medic names, and no request or
        response headers. The copy they render escapes keys too, for any
        output that does."""
        har = _har_path(
            tmp_path,
            {
                "method": "GET",
                "url": "https://api.example.com/",
                "headers": [{"name": "X-\x1bc", "value": "a\x9bb"}],
            },
        )
        report = analyze(parse_har(har.read_text(encoding="utf-8")))
        assert cli_main._escaped(report).request.headers == {"X-\\x1bc": "a\\x9bb"}
        # The report itself, which -o json renders, is unchanged.
        assert report.request.headers == {"X-\x1bc": "a\x9bb"}


# --- serve ---------------------------------------------------------------


class TestServe:
    def test_serve_calls_uvicorn_with_app_path(self, runner, monkeypatch):
        seen: dict[str, Any] = {}

        def fake_uvicorn_run(app_path, **kwargs):
            seen["app_path"] = app_path
            seen.update(kwargs)

        # uvicorn is imported inside serve(); patch the module-level attr.
        import uvicorn as _uv

        monkeypatch.setattr(_uv, "run", fake_uvicorn_run)

        result = runner.invoke(app, ["serve", "--port", "9999", "--host", "0.0.0.0"])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        assert seen["app_path"] == "api_medic.web.app:app"
        assert seen["port"] == 9999
        assert seen["host"] == "0.0.0.0"

    def test_banner_shows_the_host_escaped(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn as _uv

        monkeypatch.setattr(_uv, "run", lambda *a, **kw: None)
        result = runner.invoke(app, ["serve", "--host", "\x1bc"], color=True)
        assert result.exit_code == 0, repr(result.exception)
        assert _raw_controls(result.stderr) == []
        assert "http://\\x1bc:8765" in result.stderr


# --- end-of-phase integration test ---------------------------------------


@pytest.mark.integration
def test_bare_url_against_httpbin_produces_terminal_report(runner):
    """End-of-phase demo: `api-medic <URL>` produces a terminal report."""
    rewritten = _rewrite_bare_url(["https://httpbin.org/status/401", "--no-color"])
    result = runner.invoke(app, rewritten)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "401" in result.stdout
