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


# Mirror parser.COOKIE_FILE and parser.COOKIE_JAR.
_COOKIE_FILE = (
    "Curl command reads cookies from a file (-b/--cookie with a file name or -); "
    "api-medic reads no files. Give the cookies as -b 'NAME=VALUE'."
)
_COOKIE_JAR = (
    "Curl command writes cookies to a file (-c/--cookie-jar); "
    "api-medic writes no files. Leave it out."
)
# Mirror parser.MANY_URLS.
_MANY_URLS = (
    "Curl command gives more than one URL (--url); api-medic analyzes one request. Leave one URL."
)

# Shaped like Chrome DevTools' Copy as cURL (bash), from generateCurlCommand
# in devtools-frontend's front_end/panels/network/NetworkLogView.ts (main,
# read 2026-11-01): the URL comes as --url, with a backslash before each
# [ ] { } for curl's URL globbing, a header with an empty value as
# -H 'Name;', and each part goes on a line of its own.
_CHROME_GET = (
    "curl --url 'https://api.example.com/v1/items?page\\[size\\]=10' \\\n"
    "  -H 'accept: application/json' \\\n"
    "  -H 'x-empty;' \\\n"
    "  -b 'session=abc; theme=dark'"
)
# The same generator writes a value holding ', ! or a control character as
# $'...', escaping \ as \\, ' as \', LF as \n, CR as \r and the others as
# \u and 4 hex digits.
_CHROME_POST = (
    "curl --url 'https://api.example.com/v1/items' \\\n"
    "  -H 'content-type: application/json' \\\n"
    '  --data-raw $\'{\\n  "name": "it\\\'s",\\n  "tab": "a\\u0009b\\u0021"\\n}\''
)
# Mirror parser.ANSI_ESCAPE.
_ANSI_ESCAPE = (
    "Could not parse curl command: a $'...' string uses an escape api-medic doesn't read. "
    "It reads \\\\, \\', \\n, \\r and \\u with 4 hex digits (not \\u0000 or \\ud800-\\udfff)."
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


class TestCurlOnSeveralLines:
    """A backslash before a line break continues the line, as in bash. shlex
    kept the line break: as an argument of its own between words, which
    argparse rejected, and inside a word or double quotes."""

    @pytest.mark.parametrize(
        ("curl", "expected"),
        [
            ("curl https://api.example.com/ \\\n  -H 'X-A: b'", ("GET", {"X-A": "b"}, b"")),
            ("curl https://api.example.com/ \\\r\n  -H 'X-A: b'", ("GET", {"X-A": "b"}, b"")),
            ("curl https://api.example.com/ \\\n  -b 'a=1'", ("GET", {"Cookie": "a=1"}, b"")),
            ("curl https://api.example.com/ -b \\\n  'a=1'", ("GET", {"Cookie": "a=1"}, b"")),
            (
                "curl https://api.example.com/ \\\n  -H 'Cookie: a=1'",
                ("GET", {"Cookie": "a=1"}, b""),
            ),
            ("curl https://api.example.com/ -d a\\\nb", ("POST", {}, b"ab")),
            ('curl https://api.example.com/ -d "a\\\nb"', ("POST", {}, b"ab")),
            # An escaped backslash, then a continuation.
            ("curl https://api.example.com/ -d a\\\\\\\nb", ("POST", {}, b"a\\b")),
        ],
        ids=[
            "lf",
            "crlf",
            "cookie-option",
            "cookie-value-on-next-line",
            "cookie-header",
            "inside-a-word",
            "inside-double-quotes",
            "escaped-backslash-then-continuation",
        ],
    )
    def test_command_on_several_lines_is_one_command(
        self, curl: str, expected: tuple[str, dict[str, str], bytes]
    ) -> None:
        cap = parse_curl(curl)
        assert (cap.method, cap.headers, cap.body) == expected

    @pytest.mark.parametrize(
        ("curl", "body"),
        [
            ("curl https://api.example.com/ -d 'a\\\nb'", b"a\\\nb"),
            # An escaped backslash, then a line break.
            ("curl https://api.example.com/ -d a\\\\\n  -k", b"a\\"),
            ("curl https://api.example.com/ -d a\\\rb", b"a\rb"),
        ],
        ids=["single-quotes", "escaped-backslash", "carriage-return-alone"],
    )
    def test_backslash_and_line_break_stay_where_bash_keeps_them(
        self, curl: str, body: bytes
    ) -> None:
        assert parse_curl(curl).body == body

    def test_backslash_at_the_end_is_refused_as_before(self) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl("curl https://api.example.com/ -d a\\")
        assert str(e.value) == "Could not parse curl command: No escaped character"

    def test_only_line_continuations_is_empty(self) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(" \\\n \\\r\n ")
        assert str(e.value) == "Empty curl command."

    def test_argument_limit_counts_the_joined_arguments(self) -> None:
        # Each -k on a line of its own, indented as a browser writes them.
        # Unjoined, each backslash-newline was an argument too.
        curl = "curl https://api.example.com/" + " \\\n  -k" * (_MAX_CURL_ARGS - 2)
        started = time.perf_counter()
        cap = parse_curl(curl)
        elapsed = time.perf_counter() - started
        assert cap.url == "https://api.example.com/"
        # See test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1

    def test_continuations_at_the_limit_are_read_quickly(self) -> None:
        started = time.perf_counter()
        with pytest.raises(ValueError) as e:
            parse_curl("\\\n" * (_MAX_CURL_CHARS // 2))
        elapsed = time.perf_counter() - started
        assert str(e.value) == "Empty curl command."
        # See test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1

    def test_character_limit_counts_the_command_as_given(self) -> None:
        # One character over the limit; joined, one under it.
        head = "curl https://api.example.com/ \\\n  -d '"
        curl = head + "a" * (_MAX_CURL_CHARS - len(head)) + "'"
        assert len(curl) == _MAX_CURL_CHARS + 1
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == "Curl command is too long (limit: 50,000 characters)."


class TestCurlUrlOption:
    """uncurl doesn't know --url, which Chrome writes the URL with, so a
    command giving its URL that way was refused."""

    @pytest.mark.parametrize(
        ("curl", "expected"),
        [
            (
                "curl --url 'https://api.example.com/' -H 'X-A: b'",
                ("https://api.example.com/", {"X-A": "b"}),
            ),
            (
                "curl -H 'X-A: b' --url https://api.example.com/",
                ("https://api.example.com/", {"X-A": "b"}),
            ),
            (
                "curl --url=https://api.example.com/ -H 'X-A: b'",
                ("https://api.example.com/", {"X-A": "b"}),
            ),
            (
                _CHROME_GET,
                (
                    "https://api.example.com/v1/items?page[size]=10",
                    {
                        "accept": "application/json",
                        "x-empty": "",
                        "Cookie": "session=abc; theme=dark",
                    },
                ),
            ),
        ],
        ids=["--url", "--url-last", "--url=<value>", "chrome-copy"],
    )
    def test_url_option_is_the_url(self, curl: str, expected: tuple[str, dict[str, str]]) -> None:
        cap = parse_curl(curl)
        assert (cap.method, cap.url, cap.headers, cap.body) == ("GET", *expected, b"")

    @pytest.mark.parametrize(
        "args",
        [
            ["--url", "https://api.example.com/", "--url", "https://api.example.com/x"],
            ["--url=https://api.example.com/", "--url", "https://api.example.com/x"],
        ],
        ids=["two", "two-spellings"],
    )
    def test_several_url_options_are_refused(self, args: list[str]) -> None:
        # curl would fetch each.
        with pytest.raises(ValueError) as e:
            parse_curl(shlex.join(["curl", *args]))
        assert str(e.value) == _MANY_URLS

    @pytest.mark.parametrize(
        "args",
        [
            ["--url", "https://api.example.com/", "https://api.example.com/x"],
            ["https://api.example.com/", "--url"],
            ["--url", "-x"],
            ["https://api.example.com/", "--", "--url", "https://api.example.com/x"],
        ],
        ids=["and-a-positional-url", "--url-last", "option-after-it", "after---"],
    )
    def test_url_option_uncurl_rejects_is_refused_as_before(self, args: list[str]) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(shlex.join(["curl", *args]))
        assert str(e.value) == "Could not parse curl command (argparse rejected it)."

    @pytest.mark.parametrize("form", ["positional", "--url"])
    @pytest.mark.parametrize(
        ("url", "sent"),
        [
            ("https://api.example.com/?a\\[0\\]=1", "https://api.example.com/?a[0]=1"),
            ("https://api.example.com/\\{a,b\\}", "https://api.example.com/{a,b}"),
            # curl removes the one backslash before each; a doubled one isn't
            # special to it.
            ("https://api.example.com/?a\\\\[0\\]=1", "https://api.example.com/?a\\[0]=1"),
        ],
        ids=["brackets", "braces", "doubled-backslash"],
    )
    def test_glob_backslashes_are_removed_from_the_url(
        self, url: str, sent: str, form: str
    ) -> None:
        # curl's URL globbing reads [ ] { }; a backslash before one makes it
        # a plain character, and curl drops the backslash.
        args = [url] if form == "positional" else ["--url", url]
        assert parse_curl(shlex.join(["curl", *args])).url == sent

    def test_other_backslashes_stay_in_the_url(self) -> None:
        url = "https://api.example.com/a\\b"
        assert parse_curl(shlex.join(["curl", url])).url == url


class TestCurlAnsiCStrings:
    """Chrome writes a value holding ', ! or a control character as $'...',
    bash's ANSI-C quoting. shlex read it as $ and a single-quoted string, so
    the $ and the escapes stayed in the value, or an escaped ' ended it."""

    @pytest.mark.parametrize(
        ("curl", "expected"),
        [
            (
                "curl https://api.example.com/ -H $'x-a: it\\'s'",
                ("GET", {"x-a": "it's"}, b""),
            ),
            (
                _CHROME_POST,
                (
                    "POST",
                    {"content-type": "application/json"},
                    b'{\n  "name": "it\'s",\n  "tab": "a\tb!"\n}',
                ),
            ),
            ("curl https://api.example.com/ -d $'a\\\\b'", ("POST", {}, b"a\\b")),
            ("curl https://api.example.com/ -d $'a\\r\\nb'", ("POST", {}, b"a\r\nb")),
            ("curl https://api.example.com/ -d x$'\\n'y", ("POST", {}, b"x\ny")),
            ("curl https://api.example.com/ -d $'caf\\u00e9'", ("POST", {}, "café".encode())),
            # bash keeps the backslash before a character it reads no escape
            # from, a line break included.
            ("curl https://api.example.com/ -d $'a\\[b\\qc'", ("POST", {}, b"a\\[b\\qc")),
            ("curl https://api.example.com/ -d $'a\\\nb'", ("POST", {}, b"a\\\nb")),
            ("curl https://api.example.com/ -b $'a=it\\'s'", ("GET", {"Cookie": "a=it's"}, b"")),
        ],
        ids=[
            "header",
            "chrome-copy",
            "backslash",
            "carriage-return",
            "inside-a-word",
            "unicode",
            "other-escapes-kept",
            "backslash-newline-kept",
            "cookie-option",
        ],
    )
    def test_ansi_c_string_is_read_as_bash_reads_it(
        self, curl: str, expected: tuple[str, dict[str, str], bytes]
    ) -> None:
        cap = parse_curl(curl)
        assert (cap.method, cap.headers, cap.body) == expected

    def test_ansi_c_url_keeps_the_glob_backslashes_for_the_url(self) -> None:
        # Chrome adds them after quoting; bash keeps them, and curl drops them.
        cap = parse_curl("curl --url $'https://api.example.com/?q=it\\'s&a\\[0\\]=1'")
        assert cap.url == "https://api.example.com/?q=it's&a[0]=1"

    @pytest.mark.parametrize(
        ("curl", "body"),
        [
            ("curl https://api.example.com/ -d \"$'a'\"", b"$'a'"),
            ("curl https://api.example.com/ -d \\$'a'", b"$a"),
        ],
        ids=["in-double-quotes", "escaped-dollar"],
    )
    def test_dollar_quote_that_isnt_an_ansi_c_string(self, curl: str, body: bytes) -> None:
        assert parse_curl(curl).body == body

    @pytest.mark.parametrize(
        "escape",
        [
            "\\t",
            "\\a",
            "\\e",
            "\\?",
            '\\"',
            "\\x41",
            "\\101",
            "\\U0001F600",
            "\\cA",
            "\\u41",
            "\\u0000",
            "\\ud800",
            "\\udfff",
        ],
    )
    def test_ansi_c_escape_chrome_doesnt_write_is_refused(self, escape: str) -> None:
        # bash reads these too, but cuts a value at \u0000 and reads a
        # surrogate by the locale.
        with pytest.raises(ValueError) as e:
            parse_curl(f"curl https://api.example.com/ -d $'a{escape}z'")
        # Quotes none of the command.
        assert str(e.value) == _ANSI_ESCAPE

    @pytest.mark.parametrize(
        "curl",
        [
            "curl https://api.example.com/ -d $'a\\'",
            "curl https://api.example.com/ -d $'abc",
        ],
        ids=["escaped-quote-last", "no-quote"],
    )
    def test_unterminated_ansi_c_string_is_refused(self, curl: str) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == "Could not parse curl command: No closing quotation"

    def test_ansi_c_string_at_the_limit_is_read_quickly(self) -> None:
        # Each \' is a ' that shlex.quote writes as 5 characters, '"'"'.
        head = "curl --url 'https://api.example.com/' \\\n  --data-raw $'"
        quotes = (_MAX_CURL_CHARS - len(head) - 1) // 2
        curl = head + "\\'" * quotes + "'"
        started = time.perf_counter()
        cap = parse_curl(curl)
        elapsed = time.perf_counter() - started
        assert cap.body == b"'" * quotes
        # See test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1


class TestCurlEmptyHeader:
    """curl sends -H 'Name;' as the header Name with no value, and Chrome
    writes a header with an empty value that way. uncurl splits a header at
    a colon, which it doesn't have."""

    @pytest.mark.parametrize(
        ("curl", "headers"),
        [
            ("curl https://api.example.com/ -H 'x-empty;'", {"x-empty": ""}),
            ("curl https://api.example.com/ --header 'x-empty;'", {"x-empty": ""}),
            # Shaped like Chrome's Copy as cURL (bash); see _CHROME_GET.
            (
                "curl --url 'https://api.example.com/' \\\n  -H 'accept: */*' \\\n  -H 'x-empty;'",
                {"accept": "*/*", "x-empty": ""},
            ),
        ],
        ids=["-H", "--header", "chrome-copy"],
    )
    def test_header_ending_in_a_semicolon_is_sent_empty(
        self, curl: str, headers: dict[str, str]
    ) -> None:
        assert parse_curl(curl).headers == headers

    def test_header_with_a_value_ending_in_a_semicolon_keeps_it(self) -> None:
        cap = parse_curl("curl https://api.example.com/ -H 'X-A: b;'")
        assert cap.headers == {"X-A": "b;"}

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            # curl sends neither.
            (["-H", "X-E;x"], "not enough values to unpack (expected 2, got 1)"),
            (["-HX-E;"], "not enough values to unpack (expected 2, got 1)"),
            # uncurl reads a header starting with ":" up to its second colon.
            (["-H", ":x"], "list index out of range"),
        ],
        ids=["text-after-the-semicolon", "-H<value>", "leading-colon"],
    )
    def test_header_uncurl_cant_split_is_refused_as_before(
        self, args: list[str], message: str
    ) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args(*args))
        assert str(e.value) == f"Could not parse curl command: {message}"

    def test_option_after_the_header_option_is_left_to_uncurl(self) -> None:
        # argparse reads -x; as an option, so -H has no value.
        with pytest.raises(ValueError) as e:
            parse_curl(_curl_with_args("-H", "-x;"))
        assert str(e.value) == "Could not parse curl command (argparse rejected it)."


# Mirror parser.USER_OPTION and parser.DATA_FILE.
_USER_OPTION = (
    "Curl command sends credentials with -u/--user, which api-medic doesn't read. "
    "Give them as -H 'Authorization: ...' instead."
)
_DATA_FILE = (
    "Curl command reads data from a file (-d/--data/--data-binary with @); "
    "api-medic reads no files. Give the data as --data-raw '...'."
)


class TestCurlUserAndDataFile:
    """uncurl reads -u/--user, whose credentials parse_curl never sent, and
    a -d or --data-binary value starting with @ as the body, where curl
    reads the data from that file."""

    @pytest.mark.parametrize(
        "curl",
        [
            "curl https://api.example.com/ -u user:secret",
            "curl https://api.example.com/v1/charges \\\n  -u sk_test_x: \\\n  -d amount=2000",
            "curl https://api.example.com/ --user user:secret",
            "curl https://api.example.com/ --user=user:secret",
            "curl https://api.example.com/ -uuser:secret",
            "curl https://api.example.com/ -ku user:secret",
            "curl https://api.example.com/ --us user:secret",
        ],
        ids=["-u", "on-several-lines", "--user", "--user=<value>", "-u<value>", "-ku", "--us"],
    )
    def test_user_option_is_refused(self, curl: str) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        # Names the option and quotes none of the command.
        assert str(e.value) == _USER_OPTION

    @pytest.mark.parametrize(
        "curl",
        [
            "curl https://api.example.com/ -d @body.json",
            "curl https://api.example.com/v1/x \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -d @body.json",
            "curl https://api.example.com/ --data @body.json",
            "curl https://api.example.com/ -d@body.json",
            "curl https://api.example.com/ -d @-",
            "curl https://api.example.com/ --data-binary @body.json",
            "curl https://api.example.com/ --data-binary=@body.json",
            "curl https://api.example.com/ --data-b @body.json",
        ],
        ids=[
            "-d",
            "on-several-lines",
            "--data",
            "-d<value>",
            "stdin",
            "--data-binary",
            "--data-binary=<value>",
            "--data-b",
        ],
    )
    def test_data_file_is_refused(self, cookie_files: Path, curl: str) -> None:
        with _no_file_or_stdin() as reads, pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == _DATA_FILE
        assert reads == []

    @pytest.mark.parametrize("option", ["--data-raw", "--data-r"])
    def test_data_raw_value_starting_with_at_is_the_body(self, option: str) -> None:
        # curl reads --data-raw's value as given.
        cap = parse_curl(_curl_with_args(option, "@mention"))
        assert (cap.method, cap.body) == ("POST", b"@mention")


# Mirror parser.CMD_LINE_END, CMD_QUOTES, POWERSHELL_LINE_END and
# SPACE_AFTER_BACKSLASH.
_CMD_LINE_END = (
    "Could not parse curl command: a line ends in ^, as a Windows cmd command continues "
    "a line. api-medic reads curl commands as bash does: copy the request as cURL (bash) "
    "instead."
)
_CMD_QUOTES = (
    'Could not parse curl command: it uses ^" quotes, as a Windows cmd command does. '
    "api-medic reads curl commands as bash does: copy the request as cURL (bash) instead."
)
_POWERSHELL_LINE_END = (
    "Could not parse curl command: a line ends in `, as a PowerShell command continues a "
    "line. api-medic reads curl commands as bash does: end each line in \\ instead."
)
_SPACE_AFTER_BACKSLASH = (
    "Could not parse curl command: a line ends in a backslash followed by spaces, so the "
    "line doesn't continue. Remove the spaces after the backslash."
)
# Shaped like Chrome DevTools' Copy as cURL (cmd), from the generator
# _CHROME_GET is shaped by: each value goes in ^"...^", a " in it becomes
# ^\^", and each part goes on a line of its own, ending in ^.
_CHROME_CMD = (
    'curl --url ^"https://api.example.com/^" ^\n'
    '  -H ^"accept: */*^" ^\n'
    '  -H ^"sec-ch-ua: ^\\^"Chromium^\\^";v=^\\^"130^\\^"^"'
)


class TestCurlOtherShells:
    """A command written for Windows cmd or PowerShell, or whose line ends
    in a backslash and spaces, got the generic parse error, or, as one line
    in cmd's quotes, a URL starting with ^."""

    @pytest.mark.parametrize(
        ("curl", "message"),
        [
            (_CHROME_CMD, _CMD_LINE_END),
            (_CHROME_CMD.replace("\n", "\r\n"), _CMD_LINE_END),
            ("curl.exe https://api.example.com/ `\n  -H 'X-A: b'", _POWERSHELL_LINE_END),
            ("curl.exe https://api.example.com/ `\r\n  -H 'X-A: b'", _POWERSHELL_LINE_END),
            ("curl https://api.example.com/ \\ \n  -H 'X-A: b'", _SPACE_AFTER_BACKSLASH),
            ("curl https://api.example.com/ \\\t\r\n  -H 'X-A: b'", _SPACE_AFTER_BACKSLASH),
            # shlex finds no closing quote in cmd's ^\^"^".
            ('curl --url ^"https://api.example.com/^" -H ^"x: a^\\^"^"', _CMD_QUOTES),
        ],
        ids=[
            "cmd",
            "cmd-crlf",
            "powershell",
            "powershell-crlf",
            "space-after-backslash",
            "tab-after-backslash",
            "cmd-quotes-shlex-cant-close",
        ],
    )
    def test_unparseable_command_names_the_other_shell(self, curl: str, message: str) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        # Quotes none of the command.
        assert str(e.value) == message

    @pytest.mark.parametrize(
        "curl",
        [
            'curl --url ^"https://api.example.com/^" -H ^"accept: */*^"',
            'curl ^"https://api.example.com/^"',
        ],
        ids=["chrome-copy-on-one-line", "positional-url"],
    )
    def test_url_in_cmd_quotes_is_refused(self, curl: str) -> None:
        # cmd's quotes leave a ^ before the URL, which no URL starts with.
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == _CMD_QUOTES

    def test_other_shells_line_endings_in_a_command_that_parses(self) -> None:
        cap = parse_curl("curl https://api.example.com/ -d 'a ^\nb `\nc'")
        assert cap.body == b"a ^\nb `\nc"

    def test_uncurls_other_errors_name_the_other_shell(self) -> None:
        # uncurl fails on the header, and the ^ line ending is in the body's
        # quotes: what parse_curl looks for, it looks for without quotes.
        with pytest.raises(ValueError) as e:
            parse_curl("curl https://api.example.com/ -H ':x' -d 'a ^\nb'")
        assert str(e.value) == _CMD_LINE_END

    @pytest.mark.parametrize(
        "curl",
        [
            # cmd writes $ as ^$. After an odd number of ^\^", a bash quote
            # tracker is outside quotes, so it reads the $' as the start of
            # a $'...' string, which has no closing quote.
            'curl ^"https://api.example.com/^" ^\r\n  -H ^"x: y^" ^\r\n'
            '  --data-raw ^"^{^\\^"a^\\^":^\\^"b^$\'c^\\^"^}^"',
            # A $'...' escape api-medic doesn't read.
            "curl https://api.example.com/ ^\n  -d $'a\\tb'",
        ],
        ids=["unclosed-ansi-c-string", "ansi-c-escape"],
    )
    def test_ansi_c_errors_name_the_other_shell(self, curl: str) -> None:
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert str(e.value) == _CMD_LINE_END


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
