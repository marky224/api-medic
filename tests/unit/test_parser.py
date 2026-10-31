"""Tests for core.parser — HAR + curl normalisation into CapturedRequest."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import uncurl  # type: ignore[import-untyped]

from api_medic.core import parser as core_parser
from api_medic.core.models import UNENCODABLE_TEXT
from api_medic.core.parser import parse_curl, parse_har


def _minimal_har(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "request": {
            "method": "GET",
            "url": "https://api.example.com/v1/health",
            "headers": [],
        },
        "timings": {},
    }
    entry.update(overrides)
    return {"log": {"version": "1.2", "entries": [entry]}}


class TestParseHar:
    def test_basic_post_with_body_and_response(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://api.example.com/v1/users",
                            "headers": [
                                {"name": "Authorization", "value": "Bearer xxx"},
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "postData": {"text": '{"name":"alex"}'},
                        },
                        "response": {
                            "status": 401,
                            "statusText": "Unauthorized",
                            "headers": [
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "content": {"text": '{"error":"invalid_token"}'},
                            "httpVersion": "HTTP/2",
                        },
                        "timings": {
                            "dns": 12.0,
                            "connect": 45.0,
                            "ssl": 78.0,
                            "wait": 95.0,
                            "receive": 17.0,
                        },
                    },
                ],
            },
        }
        cap = parse_har(har)
        assert cap.method == "POST"
        assert cap.url == "https://api.example.com/v1/users"
        assert cap.headers["Authorization"] == "Bearer xxx"
        assert cap.body == b'{"name":"alex"}'
        assert cap.source == "har"
        assert cap.response is not None
        assert cap.response.status_code == 401
        assert cap.response.status_text == "Unauthorized"
        assert cap.response.protocol == "HTTP/2"
        assert cap.timing.dns_ms == 12.0
        assert cap.timing.tls_ms == 78.0
        assert cap.timing.total_ms == pytest.approx(247.0)

    def test_accepts_string_input(self):
        cap = parse_har(json.dumps(_minimal_har()))
        assert cap.method == "GET"

    def test_rejects_non_har_dict(self):
        with pytest.raises(ValueError, match="Not a HAR archive"):
            parse_har({"foo": "bar"})

    def test_rejects_empty_entries(self):
        with pytest.raises(ValueError, match="no entries"):
            parse_har({"log": {"entries": []}})

    def test_rejects_entry_without_request(self):
        with pytest.raises(ValueError, match="missing 'request'"):
            parse_har({"log": {"entries": [{"response": {"status": 200}}]}})

    def test_handles_missing_timings(self):
        cap = parse_har(_minimal_har())
        assert cap.timing.dns_ms is None
        assert cap.timing.total_ms is None

    def test_negative_timings_treated_as_unmeasured(self):
        # HAR spec: -1 means a phase wasn't measured
        har = _minimal_har(
            timings={"dns": -1, "connect": 22.0, "ssl": -1, "wait": 41.0, "receive": 5.0}
        )
        cap = parse_har(har)
        assert cap.timing.dns_ms is None
        assert cap.timing.tls_ms is None
        assert cap.timing.connect_ms == 22.0
        assert cap.timing.total_ms == pytest.approx(68.0)

    @pytest.mark.parametrize("dns", [float("inf"), 10**400], ids=["infinity", "400-digit-int"])
    def test_timing_no_finite_float_holds_is_unmeasured(self, dns: float) -> None:
        # JSON's 1e400 reads as Infinity; float() of the integer overflows.
        cap = parse_har(_minimal_har(timings={"dns": dns, "connect": 22.0, "wait": 41.0}))
        assert cap.timing.dns_ms is None
        assert cap.timing.connect_ms == 22.0
        assert cap.timing.total_ms == pytest.approx(63.0)

    def test_total_past_the_float_maximum_is_unmeasured(self) -> None:
        # Each phase is finite; their sum isn't.
        cap = parse_har(_minimal_har(timings={"dns": 1e308, "wait": 1e308}))
        assert cap.timing.dns_ms == 1e308
        assert cap.timing.ttfb_ms == 1e308
        assert cap.timing.total_ms is None

    def test_omits_response_when_missing_or_zero_status(self):
        har = _minimal_har()
        cap = parse_har(har)
        assert cap.response is None

    def test_method_normalized_to_upper(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["method"] = "post"
        cap = parse_har(har)
        assert cap.method == "POST"

    def test_missing_request_method_raises_value_error(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {"url": "https://x/"},
                        "response": {"status": 200},
                    },
                ],
            },
        }
        with pytest.raises(ValueError, match=r"request\.method is missing"):
            parse_har(har)

    def test_empty_request_method_raises_value_error(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["method"] = ""
        with pytest.raises(ValueError, match=r"request\.method is empty"):
            parse_har(har)

    def test_wrong_type_request_method_raises_value_error(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["method"] = 42
        with pytest.raises(ValueError, match=r"request\.method must be a string, got int"):
            parse_har(har)

    def test_missing_request_url_raises_value_error(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {"method": "GET"},
                        "response": {"status": 200},
                    },
                ],
            },
        }
        with pytest.raises(ValueError, match=r"request\.url is missing"):
            parse_har(har)

    def test_empty_request_url_raises_value_error(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["url"] = ""
        with pytest.raises(ValueError, match=r"request\.url is empty"):
            parse_har(har)

    def test_wrong_type_request_url_raises_value_error(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["url"] = ["not", "a", "string"]
        with pytest.raises(ValueError, match=r"request\.url must be a string, got list"):
            parse_har(har)

    def test_error_message_includes_entry_url_when_method_invalid(self):
        # When the URL is parseable but the method is not, the user wants to
        # know *which* of the (potentially many) HAR entries failed. v1 only
        # parses entries[0], so the URL is the load-bearing identifier.
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["url"] = "https://api.example.com/foo"
        har["log"]["entries"][0]["request"]["method"] = ""
        with pytest.raises(
            ValueError,
            match=r"HAR entry\[0\] \(https://api\.example\.com/foo\): "
            r"request\.method is empty",
        ):
            parse_har(har)

    def test_error_label_falls_back_when_url_also_missing(self):
        # Method *and* url both missing: label can't echo a URL, so the
        # message degrades to "HAR entry[0]:" without a URL parenthetical.
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {"request": {"headers": []}, "response": {"status": 200}},
                ],
            },
        }
        with pytest.raises(ValueError, match=r"^HAR entry\[0\]: request\.method is missing"):
            parse_har(har)

    def test_non_dict_request_raises_value_error(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [{"request": "not-an-object"}],
            },
        }
        with pytest.raises(ValueError, match="'request' must be an object"):
            parse_har(har)

    def test_non_integer_response_status_raises_value_error(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://x/"},
                        "response": {"status": "not-a-number"},
                    },
                ],
            },
        }
        with pytest.raises(ValueError, match=r"response\.status is not an integer"):
            parse_har(har)

    @pytest.mark.parametrize("value", [None, "", [], 0, False, {}])
    @pytest.mark.parametrize("field", ["request.postData", "response.content", "timings"])
    def test_falsy_object_field_parses_as_if_absent(self, field: str, value: Any) -> None:
        # Only a field of another type that isn't falsy is an error.
        def har(with_field: bool) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "request": {"method": "POST", "url": "https://x/"},
                "response": {"status": 200},
            }
            if with_field:
                parent, _, name = field.rpartition(".")
                (entry[parent] if parent else entry)[name] = value
            return {"log": {"entries": [entry]}}

        assert parse_har(har(True)) == parse_har(har(False))

    def test_chrome_devtools_401_entry_yields_auth_missing(self):
        """Regression: a chrome.devtools.network.Request-shaped 401 entry
        (as the browser extension serializes it) round-trips through the
        parser and engine to produce the same `auth.missing` finding the
        CLI's run subcommand emits for the equivalent request. The
        extension's analyze pathway is contractually byte-identical to
        the CLI's (per the architectural invariant) — this guards that.
        """
        from api_medic.core.engine import analyze

        # Shape mirrors the extension's buildAnalyzePayload output for an
        # unauthenticated GET to httpbin.org/status/401.
        entry = {
            "request": {
                "method": "GET",
                "url": "https://httpbin.org/status/401",
                "headers": [
                    {"name": "Accept", "value": "*/*"},
                    {"name": "User-Agent", "value": "Mozilla/5.0"},
                ],
                "cookies": [],
                "queryString": [],
                "headersSize": -1,
                "bodySize": 0,
                "httpVersion": "HTTP/1.1",
            },
            "response": {
                "status": 401,
                "statusText": "UNAUTHORIZED",
                "headers": [
                    {"name": "WWW-Authenticate", "value": 'Basic realm="Fake Realm"'},
                    {"name": "Content-Type", "value": "text/html"},
                    {"name": "Content-Length", "value": "0"},
                ],
                "cookies": [],
                "content": {"size": 0, "mimeType": "text/html"},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": 0,
                "httpVersion": "HTTP/1.1",
            },
            "startedDateTime": "2026-05-01T12:00:00Z",
            "time": 150,
            "timings": {"send": 0, "wait": 100, "receive": 50},
            "cache": {},
        }
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "api-medic-extension", "version": "0.1.0"},
                "entries": [entry],
            },
        }

        captured = parse_har(har)
        assert captured.response is not None
        assert captured.response.status_code == 401

        report = analyze(captured)
        ids = [f.id for f in report.findings]
        assert "auth.missing" in ids

    @pytest.mark.parametrize(
        "raw_http_version,expected",
        [
            ("http/2.0", "HTTP/2"),  # Chromium HAR format
            ("HTTP/2.0", "HTTP/2"),  # Firefox HAR format
            ("http/2", "HTTP/2"),
            ("h2", "HTTP/2"),  # ALPN identifier
            ("http/1.1", "HTTP/1.1"),
            ("HTTP/1.1", "HTTP/1.1"),
            ("http/1.0", "HTTP/1.0"),
            ("h3", "HTTP/3"),
            ("http/3.0", "HTTP/3"),
        ],
    )
    def test_normalizes_http_version_to_runner_format(self, raw_http_version, expected):
        # Browser HARs disagree on httpVersion casing/formatting (Chromium
        # 'http/2.0', Firefox 'HTTP/2.0', some tools use ALPN 'h2'). httpx's
        # response.http_version is always 'HTTP/1.1' or 'HTTP/2'. Normalising
        # on parse keeps Reports visually consistent across surfaces.
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {
            "status": 200,
            "headers": [],
            "httpVersion": raw_http_version,
        }
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == expected

    def test_unknown_http_version_passes_through(self):
        # Don't misrepresent values we don't recognise — a surprising display
        # is better than a wrong one.
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {
            "status": 200,
            "headers": [],
            "httpVersion": "QUIC-v3-experimental",
        }
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == "QUIC-v3-experimental"

    def test_missing_http_version_defaults_to_http_1_1(self):
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {"status": 200, "headers": []}
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == "HTTP/1.1"

    def test_whitespace_only_http_version_defaults_to_http_1_1(self):
        # Whitespace-only is treated like empty rather than passed through —
        # otherwise the Report would render leaky whitespace in the Protocol cell.
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {
            "status": 200,
            "headers": [],
            "httpVersion": "   ",
        }
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == "HTTP/1.1"

    def test_normalizes_recognized_http_version_with_trailing_whitespace(self):
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {
            "status": 200,
            "headers": [],
            "httpVersion": "  http/2.0  ",
        }
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == "HTTP/2"

    def test_unknown_http_version_pass_through_strips_whitespace(self):
        # Unknown values pass through, but stripped — leaking surrounding
        # whitespace into the Protocol display would be cosmetically broken.
        har = _minimal_har()
        har["log"]["entries"][0]["response"] = {
            "status": 200,
            "headers": [],
            "httpVersion": "  QUIC-experimental  ",
        }
        cap = parse_har(har)
        assert cap.response is not None
        assert cap.response.protocol == "QUIC-experimental"


# Mirror parser.MAX_CURL_CHARS, MAX_CURL_ARGS and MAX_CURL_COOKIE_CHARS.
_MAX_CURL_CHARS = 50_000
_MAX_CURL_ARGS = 1_000
_MAX_CURL_COOKIE_CHARS = 4_096
_CURL_HEAD = "curl https://api.example.com/ -H 'Cookie: session=SECRET-7f3a' -d '"


def _curl_of_length(n: int) -> str:
    """A curl command exactly `n` characters long, padded with -d data."""
    return _CURL_HEAD + "a" * (n - len(_CURL_HEAD) - 1) + "'"


def _curl_of_args(n: int) -> str:
    """A curl command of exactly `n` arguments: curl, the URL and -k flags."""
    return "curl https://api.example.com/" + " -k" * (n - 2)


def _cookie_value(n: int) -> str:
    """A Cookie header value exactly `n` characters long, as uncurl splits it
    from the key: "=" repeated, then an emoji, which makes Python store the
    string at 4 bytes a character. SimpleCookie is slowest on it of the values
    tried."""
    return "=" * (n - 1) + "\U0001f600"


# The ways a command gives its Cookie header's value, each given that value:
# the ways argparse reads a -H value, and curl's -b/--cookie, whose values
# count even when a Cookie header replaces them.
_COOKIE_HEADER_FORMS: dict[str, Callable[[str], list[str]]] = {
    "-H": lambda v: ["-H", "Cookie:" + v],
    "-H<value>": lambda v: ["-HCookie:" + v],
    "-kH<value>": lambda v: ["-kHCookie:" + v],
    "-H=<value>": lambda v: ["-H=Cookie:" + v],
    "-kH=<value>": lambda v: ["-kH=Cookie:" + v],
    "--header": lambda v: ["--header", "Cookie:" + v],
    "--header=<value>": lambda v: ["--header=Cookie:" + v],
    "--hea": lambda v: ["--hea", "Cookie:" + v],
    "--hea=<value>": lambda v: ["--hea=Cookie:" + v],
    "$COOKIE$-key": lambda v: ["-H", "$COOKIE$:" + v],
    "value-starting-with-colon": lambda v: ["-H", "Cookie::" + v[1:]],
    "two-headers": lambda v: ["-H", "Cookie:" + v[:100], "-H", "cookie:" + v[100:]],
    "-b": lambda v: ["-b", v],
    "-b<value>": lambda v: ["-b" + v],
    "-kb<value>": lambda v: ["-kb" + v],
    "--cookie": lambda v: ["--cookie", v],
    "--cookie=<value>": lambda v: ["--cookie=" + v],
    "two-b": lambda v: ["-b", v[:100], "-b", v[100:]],
    "header-and-b": lambda v: ["-H", "Cookie:" + v[:100], "-b", v[100:]],
}

# Whether this Python's argparse reads -kH=<value> as <value>, as the local
# Python 3.12's does.
_ARGPARSE_DROPS_THE_EQUALS_AFTER_FLAGS = uncurl.api.parser.parse_args(
    ["curl", "https://api.example.com/", "-kH=a:b"]
).header == ["a:b"]


def _curl_with_cookie(value: str, form: str = "-H") -> str:
    return shlex.join(["curl", "https://api.example.com/", *_COOKIE_HEADER_FORMS[form](value)])


class TestParseCurl:
    def test_post_with_data_and_headers(self):
        curl = (
            "curl -X POST 'https://api.example.com/v1/users' "
            "-H 'Authorization: Bearer xxx' "
            "-H 'Content-Type: application/json' "
            '--data \'{"name":"alex"}\''
        )
        cap = parse_curl(curl)
        assert cap.method == "POST"
        assert cap.url == "https://api.example.com/v1/users"
        assert cap.headers["Authorization"] == "Bearer xxx"
        assert cap.headers["Content-Type"] == "application/json"
        assert cap.body == b'{"name":"alex"}'
        assert cap.source == "curl"
        assert cap.response is None

    def test_get_default_method(self):
        cap = parse_curl("curl https://api.example.com/health")
        assert cap.method == "GET"
        assert cap.url == "https://api.example.com/health"
        assert cap.body == b""

    def test_empty_curl_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_curl("   ")

    def test_garbage_input_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_curl("definitely not a curl command")

    def test_rejected_command_writes_nothing_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse would print the unrecognized --data-urlencode and its value.
        with pytest.raises(ValueError, match="argparse rejected it"):
            parse_curl("curl https://api.example.com/ --data-urlencode 'session=SECRET-7f3a'")
        assert capsys.readouterr().err == ""

    def test_command_at_the_limit_parses(self) -> None:
        cap = parse_curl(_curl_of_length(_MAX_CURL_CHARS))
        assert cap.url == "https://api.example.com/"
        assert cap.body == b"a" * (_MAX_CURL_CHARS - len(_CURL_HEAD) - 1)

    def test_command_over_the_limit_raises_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # shlex, which uncurl splits the command with, slows down
        # quadratically: a 1,000,000-character command took over 10 seconds.
        def parse_context(*a: Any, **kw: Any) -> Any:
            raise AssertionError("parsed the command")

        monkeypatch.setattr(uncurl, "parse_context", parse_context)
        with pytest.raises(ValueError) as e:
            parse_curl(_curl_of_length(_MAX_CURL_CHARS + 1))
        # Names the limit and quotes none of the command.
        assert str(e.value) == "Curl command is too long (limit: 50,000 characters)."

    def test_command_at_the_argument_limit_parses_quickly(self) -> None:
        started = time.perf_counter()
        cap = parse_curl(_curl_of_args(_MAX_CURL_ARGS))
        elapsed = time.perf_counter() - started
        assert cap.url == "https://api.example.com/"
        # On Python 3.12+ a trace function, such as coverage's under CI's
        # --cov, slows uncurl's argparse loop from 0.05 s to about 6 s
        # locally, so the bound is checked only when nothing is tracing.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1

    def test_command_over_the_argument_limit_raises_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # argparse, which uncurl reads the arguments with, slows down with
        # the square of the number of options: 33,323 -k flags, 100,000
        # characters, took 21 seconds.
        def parse_context(*a: Any, **kw: Any) -> Any:
            raise AssertionError("parsed the command")

        monkeypatch.setattr(uncurl, "parse_context", parse_context)
        with pytest.raises(ValueError) as e:
            parse_curl(_curl_of_args(_MAX_CURL_ARGS + 1))
        # Names the limit and quotes none of the command.
        assert str(e.value) == "Curl command has too many arguments (limit: 1,000)."

    def test_cookie_header_at_the_limit_parses(self) -> None:
        # The slowest value found takes about half a second locally; the
        # bound leaves room for a slower CI runner.
        started = time.perf_counter()
        cap = parse_curl(_curl_with_cookie(_cookie_value(_MAX_CURL_COOKIE_CHARS)))
        assert time.perf_counter() - started < 3
        assert cap.url == "https://api.example.com/"

    @pytest.mark.parametrize("form", list(_COOKIE_HEADER_FORMS))
    def test_cookie_header_over_the_limit_raises_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch, form: str
    ) -> None:
        # uncurl reads each Cookie header value with http.cookies.SimpleCookie,
        # which slows down with the square of its length: a 40,000-character
        # command whose Cookie value is a=a=..." took 18 seconds locally.
        if form == "-kH=<value>" and not _ARGPARSE_DROPS_THE_EQUALS_AFTER_FLAGS:
            pytest.skip("this Python's argparse doesn't read -kH=<value> as <value>")

        def parse_context(*a: Any, **kw: Any) -> Any:
            raise AssertionError("parsed the command")

        monkeypatch.setattr(uncurl, "parse_context", parse_context)
        with pytest.raises(ValueError) as e:
            parse_curl(_curl_with_cookie(_cookie_value(_MAX_CURL_COOKIE_CHARS + 1), form))
        # Names the limit and quotes none of the command.
        assert str(e.value) == (
            "Curl command's Cookie header is too long (limit: 4,096 characters)."
        )

    def test_other_header_over_the_cookie_limit_parses(self) -> None:
        value = _cookie_value(_MAX_CURL_COOKIE_CHARS + 1)
        curl = shlex.join(["curl", "https://api.example.com/", "-H", "X-Cookie:" + value])
        assert parse_curl(curl).headers["X-Cookie"] == value

    def test_cookie_check_reads_the_arguments_with_uncurls_parser(self) -> None:
        assert uncurl.parse_context.__globals__["parser"] is uncurl.api.parser

    def test_unclosed_quote_gives_the_parse_error(self) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl("curl https://api.example.com/ -d 'open")
        assert str(e.value) == "Could not parse curl command: No closing quotation"

    @pytest.mark.parametrize(
        ("args", "cookie"),
        [
            (["-H", "Cookie: session=abc; theme=dark"], "session=abc; theme=dark"),
            # SimpleCookie would sort these and drop the attribute.
            (["-H", "Cookie: b=2; a=1; Path=/"], "b=2; a=1; Path=/"),
            (["-H", "Cookie: a=1", "-H", "cookie: b=2"], "a=1; b=2"),
            (["-HCookie: a=1"], "a=1"),
            (["--header=Cookie: a=1"], "a=1"),
            (["-H", "$Cookie:a=1"], "a=1"),
        ],
        ids=[
            "one",
            "order-and-attributes",
            "two-headers",
            "-H<value>",
            "--header=<value>",
            "$-key",
        ],
    )
    def test_cookie_header_is_kept_as_given(self, args: list[str], cookie: str) -> None:
        # uncurl moves Cookie headers into ctx.cookies; the request didn't carry them.
        cap = parse_curl(shlex.join(["curl", "https://api.example.com/", *args, "-H", "X-A: b"]))
        assert cap.headers == {"X-A": "b", "Cookie": cookie}

    def test_command_without_a_cookie_header_keeps_uncurls_headers(self) -> None:
        curl = shlex.join(
            [
                "curl",
                "https://api.example.com/",
                "-H",
                "Authorization: Bearer x",
                "-H",
                "X-Cookie: a=1",
                "-H",
                "Accept :  */*  ",
                "-H",
                ":authority: api.example.com",
            ]
        )
        headers = dict(uncurl.parse_context(curl).headers)
        assert list(parse_curl(curl).headers.items()) == list(headers.items())

    @pytest.mark.parametrize(
        "curl",
        [
            "curl https://api.example.com/ \\\n  -H 'X-A: b'",
            "curl https://api.example.com/ \\\n  -b 'a=1'",
        ],
        ids=["header", "cookie-option"],
    )
    def test_command_on_several_lines_is_refused_as_before(self, curl: str) -> None:
        # Neither uncurl nor parse_curl joins a backslash-newline continuation.
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == "Could not parse curl command (argparse rejected it)."


# Mirror parser.COOKIE_FILE and parser.COOKIE_JAR.
_COOKIE_FILE = (
    "Curl command reads cookies from a file (-b/--cookie with a file name or -); "
    "api-medic reads no files. Give the cookies as -b 'NAME=VALUE'."
)
_COOKIE_JAR = (
    "Curl command writes cookies to a file (-c/--cookie-jar); "
    "api-medic writes no files. Leave it out."
)


def _curl_with_args(*args: str) -> str:
    return shlex.join(["curl", "https://api.example.com/", *args])


class _UnreadableStdin:
    """A stdin that records, and fails, every read."""

    def __init__(self, reads: list[str]) -> None:
        self._reads = reads

    def read(self, *a: Any) -> str:
        self._reads.append("stdin")
        raise AssertionError("read stdin")

    readline = readlines = read

    def __iter__(self) -> Iterator[str]:
        return iter([self.read()])


@contextlib.contextmanager
def _no_file_or_stdin() -> Iterator[list[str]]:
    """Record, and fail, every file open and stdin read in the block."""
    reads: list[str] = []

    def fail(*a: Any, **kw: Any) -> Any:
        reads.append(f"open {a[0]!r}")
        raise AssertionError("opened a file")

    with (
        mock.patch("builtins.open", fail),
        mock.patch("io.open", fail),
        mock.patch("os.open", fail),
        mock.patch("sys.stdin", _UnreadableStdin(reads)),
    ):
        yield reads


@pytest.fixture
def cookie_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory holding the files the tests' options name."""
    for name in ("cookies.txt", "jar.txt", "x"):
        (tmp_path / name).write_text("api.example.com\tFALSE\t/\tTRUE\t0\tsession\tabc\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCurlCookieOptions:
    """uncurl reads curl's -b as --data-binary, so a cookie became the body,
    and it doesn't know --cookie or -c."""

    @pytest.mark.parametrize(
        ("args", "cookie"),
        [
            (["-b", "session=abc; theme=dark"], "session=abc; theme=dark"),
            (["-bsession=abc"], "session=abc"),
            (["--cookie", "a=1"], "a=1"),
            (["--cookie=a=1"], "a=1"),
            (["-kb", "a=1"], "a=1"),
            (["-kba=1"], "a=1"),
            (["-sib", "a=1"], "a=1"),
            # curl keeps the "=" after a short option; argparse would drop it.
            (["-b=a=1"], "=a=1"),
            (["-b", "a=1", "--cookie", "b=2"], "a=1; b=2"),
            (["-b", " a=1 "], "a=1"),
            # The next argument is the value whatever it starts with, as in curl.
            (["-b", "-x=1"], "-x=1"),
        ],
        ids=[
            "-b",
            "-b<value>",
            "--cookie",
            "--cookie=<value>",
            "-kb",
            "-kb<value>",
            "-sib",
            "-b=<value>",
            "several",
            "whitespace",
            "value-starting-with-dash",
        ],
    )
    def test_cookie_option_is_the_cookie_header(self, args: list[str], cookie: str) -> None:
        cap = parse_curl(_curl_with_args(*args))
        assert (cap.method, cap.headers, cap.body) == ("GET", {"Cookie": cookie}, b"")

    def test_cookie_option_keeps_the_method_and_body(self) -> None:
        cap = parse_curl(_curl_with_args("-X", "PUT", "-d", "x", "-b", "a=1"))
        assert (cap.method, cap.headers, cap.body) == ("PUT", {"Cookie": "a=1"}, b"x")

    @pytest.mark.parametrize("option", ["--data-binary", "--data-raw", "--data-b"])
    def test_data_binary_and_data_raw_stay_bodies(self, option: str) -> None:
        cap = parse_curl(_curl_with_args(option, "a=1"))
        assert (cap.method, cap.headers, cap.body) == ("POST", {}, b"a=1")

    @pytest.mark.parametrize(
        ("args", "headers"),
        [
            (["-H", "Cookie: a=1", "-b", "b=2"], {"Cookie": "a=1"}),
            (["-b", "b=2", "-H", "Cookie: a=1"], {"Cookie": "a=1"}),
            (["-H", "Cookie:", "-b", "b=2"], {}),
        ],
        ids=["header-first", "option-first", "empty-header"],
    )
    def test_cookie_header_replaces_the_cookie_option(
        self, args: list[str], headers: dict[str, str]
    ) -> None:
        # curl 8.5.0 sends only the -H Cookie header, and no Cookie header
        # at all for an empty one.
        cap = parse_curl(_curl_with_args(*args))
        assert (cap.method, cap.headers, cap.body) == ("GET", headers, b"")

    @pytest.mark.parametrize(
        "args",
        [
            ["-b", "cookies.txt"],
            ["-bcookies.txt"],
            ["-kb", "cookies.txt"],
            ["--cookie", "cookies.txt"],
            ["--cookie=cookies.txt"],
            ["-b", "-"],
            ["-b", ""],
            ["-b", "@c=1"],
            ["-b", "a=1", "-b", "cookies.txt"],
        ],
        ids=[
            "-b",
            "-b<value>",
            "-kb",
            "--cookie",
            "--cookie=<value>",
            "stdin",
            "empty",
            "@-name",
            "after-a-cookie",
        ],
    )
    def test_cookie_file_is_refused(self, cookie_files: Path, args: list[str]) -> None:
        with _no_file_or_stdin() as reads, pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args(*args))
        # Names the option and quotes none of the command.
        assert str(e.value) == _COOKIE_FILE
        assert reads == []

    @pytest.mark.parametrize(
        "args",
        [
            ["-c", "jar.txt"],
            ["-cjar.txt"],
            ["-kc", "jar.txt"],
            ["--cookie-jar", "jar.txt"],
            ["--cookie-jar=jar.txt"],
            ["-c", "-"],
            ["-b", "a=1", "-c", "jar.txt"],
        ],
        ids=["-c", "-c<value>", "-kc", "--cookie-jar", "--cookie-jar=<value>", "stdout", "with-b"],
    )
    def test_cookie_jar_is_refused(self, cookie_files: Path, args: list[str]) -> None:
        jar = (cookie_files / "jar.txt").read_text()
        with _no_file_or_stdin() as reads, pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args(*args))
        assert str(e.value) == _COOKIE_JAR
        assert reads == []
        assert (cookie_files / "jar.txt").read_text() == jar

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            # argparse reads -ba=1 as an option, so -d has no value.
            (["-d", "-ba=1"], "Could not parse curl command (argparse rejected it)."),
            # And -bx as -b naming the file x, which curl would send as -H's value.
            (["-H", "-bx"], _COOKIE_FILE),
            (["-X", "-b", "a=1"], "Could not parse curl command (argparse rejected it)."),
            (["-H", "-c", "x"], _COOKIE_JAR),
        ],
        ids=["-d-then-cookie", "-H-then-file", "-X-then-cookie", "-H-then-jar"],
    )
    def test_option_value_that_looks_like_a_cookie_option(
        self, cookie_files: Path, args: list[str], message: str
    ) -> None:
        with _no_file_or_stdin() as reads, pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args(*args))
        assert str(e.value) == message
        assert reads == []

    @pytest.mark.parametrize(
        "args",
        [
            ["-b"],
            ["--cookie"],
            ["-c"],
            ["--", "-b", "a=1"],
            ["-j", "-b", "a=1"],
            ["--junk-session-cookies", "-b", "a=1"],
        ],
        ids=["-b-last", "--cookie-last", "-c-last", "after---", "-j", "--junk-session-cookies"],
    )
    def test_command_uncurl_rejects_is_refused_as_before(
        self, cookie_files: Path, args: list[str]
    ) -> None:
        with _no_file_or_stdin() as reads, pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args(*args))
        assert str(e.value) == "Could not parse curl command (argparse rejected it)."
        assert reads == []

    def test_cookie_option_at_the_limit_parses(self) -> None:
        value = _cookie_value(_MAX_CURL_COOKIE_CHARS)
        cap = parse_curl(_curl_with_cookie(value, "-b"))
        assert (cap.method, cap.headers, cap.body) == ("GET", {"Cookie": value}, b"")

    def test_command_with_a_cookie_option_at_the_limit_parses_quickly(self) -> None:
        # uncurl gets the command written again by shlex.join, which writes
        # each single quote as 5 characters: '"'"'.
        head = 'curl https://api.example.com/ -b a=1 -d "'
        curl = head + "'" * (_MAX_CURL_CHARS - len(head) - 1) + '"'
        started = time.perf_counter()
        cap = parse_curl(curl)
        elapsed = time.perf_counter() - started
        assert cap.body == b"'" * (_MAX_CURL_CHARS - len(head) - 1)
        assert cap.headers == {"Cookie": "a=1"}
        # See test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1

    def test_cookie_option_flags_are_uncurls_flags(self) -> None:
        # A flag uncurl reads without a value that isn't here would let a
        # token like -Lb reach uncurl, which would read its cookies as the body.
        flags = {
            option[1]
            for action in uncurl.api.parser._actions
            if action.nargs == 0
            for option in action.option_strings
            if len(option) == 2 and option != "-h"
        }
        assert set(core_parser._CURL_FLAGS) == flags


# HAR or curl body text holding an unpaired surrogate, which UTF-8 can't
# encode. parse_har and parse_curl used to raise UnicodeEncodeError, and its
# message was the 400's detail.
_UNENCODABLE_BODY_TEXT: list[dict[str, Any]] = [
    {
        "kind": "har",
        "har": {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://api.example.com/",
                            "postData": {"mimeType": "text/plain", "text": "a\ud800"},
                        }
                    }
                ]
            }
        },
    },
    {
        "kind": "har",
        "har": {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://api.example.com/"},
                        "response": {"status": 200, "content": {"text": "\ud800"}},
                    }
                ]
            }
        },
    },
    {"kind": "curl", "curl": "curl https://api.example.com/ -d 'a\ud800'"},
]
_UNENCODABLE_BODY_TEXT_IDS = ["har-post-data", "har-response-content", "curl-data"]


class TestUnencodableBodyText:
    @pytest.mark.parametrize("payload", _UNENCODABLE_BODY_TEXT, ids=_UNENCODABLE_BODY_TEXT_IDS)
    def test_raises_the_unencodable_text_error(self, payload: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match=f"^{re.escape(UNENCODABLE_TEXT)}$"):
            if payload["kind"] == "har":
                parse_har(payload["har"])
            else:
                parse_curl(payload["curl"])

    def test_text_without_a_surrogate_is_encoded_as_before(self) -> None:
        har = _minimal_har(
            request={
                "method": "POST",
                "url": "https://api.example.com/",
                "postData": {"text": "caf\u00e9"},
            },
            response={"status": 200, "content": {"text": "\u2603"}},
        )
        captured = parse_har(har)
        assert captured.body == "caf\u00e9".encode()
        assert captured.response is not None
        assert captured.response.body == "\u2603".encode()
        assert parse_curl("curl https://x.com/ -d 'caf\u00e9'").body == "caf\u00e9".encode()
