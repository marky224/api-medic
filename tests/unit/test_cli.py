"""Tests for cli.main using typer.testing.CliRunner.

The runner subcommand and bare-URL paths monkey-patch `runner_run` to
return a synthetic CapturedRequest so tests stay offline. One
@integration test at the bottom hits httpbin for the end-of-phase demo.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from api_medic.cli import main as cli_main
from api_medic.cli.main import _rewrite_bare_url, app
from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.models import TimingBreakdown


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
    calls: list[dict] = []

    def fake_run(method: str, url: str, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return _captured(method=method.upper(), url=url, headers=kwargs.get("headers") or {})

    monkeypatch.setattr(cli_main, "runner_run", fake_run)
    return calls


# --- Basics ---------------------------------------------------------------


class TestVersionAndHelp:
    def test_version_prints_and_exits(self, runner):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "api-medic" in result.stdout

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


# --- serve ---------------------------------------------------------------


class TestServe:
    def test_serve_calls_uvicorn_with_app_path(self, runner, monkeypatch):
        seen: dict = {}

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


# --- end-of-phase integration test ---------------------------------------


@pytest.mark.integration
def test_bare_url_against_httpbin_produces_terminal_report(runner):
    """End-of-phase demo: `api-medic <URL>` produces a terminal report."""
    rewritten = _rewrite_bare_url(["https://httpbin.org/status/401", "--no-color"])
    result = runner.invoke(app, rewritten)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "401" in result.stdout
