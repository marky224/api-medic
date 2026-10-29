"""Tests for the AWS Lambda handler under deploy/lambda/handler.py.

handler.py lives outside the api_medic package by architectural choice
(it's a separate deployable unit). Tests put deploy/lambda/ on sys.path
to import it.
"""

from __future__ import annotations

import base64
import json
import logging
import shlex
import socket
import ssl
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from api_medic import __version__
from api_medic.core.captured import CapturedDns, CapturedRequest
from api_medic.core.engine import analyze
from api_medic.core.models import Finding, Report
from api_medic.core.parser import parse_curl, parse_har
from api_medic.core.runner import run

_DEPLOY_LAMBDA = Path(__file__).parent.parent.parent / "deploy" / "lambda"
sys.path.insert(0, str(_DEPLOY_LAMBDA))

import handler  # noqa: E402  — sys.path manipulated above


def _event(
    method: str = "GET",
    path: str = "/api/health",
    body: str | None = None,
    is_base64: bool = False,
) -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "body": body,
        "isBase64Encoded": is_base64,
    }


class TestHealth:
    def test_ok(self):
        result = handler.lambda_handler(_event("GET", "/api/health"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ok"] is True
        assert body["version"] == __version__


class TestAnalyzeHar:
    def _har_event(self, har: dict[str, Any]) -> dict[str, Any]:
        return _event("POST", "/api/analyze", json.dumps({"kind": "har", "har": har}))

    def test_valid_har_returns_report(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.example.com/v1/health",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "headers": [],
                            "httpVersion": "HTTP/2",
                        },
                        "timings": {},
                    }
                ],
            }
        }
        result = handler.lambda_handler(self._har_event(har), None)
        assert result["statusCode"] == 200
        report = json.loads(result["body"])
        assert report["source"] == "har"
        assert report["request"]["method"] == "GET"
        assert report["response"]["status_code"] == 200

    def test_missing_har_field_400(self):
        result = handler.lambda_handler(
            _event("POST", "/api/analyze", json.dumps({"kind": "har"})), None
        )
        assert result["statusCode"] == 400
        assert "Missing 'har'" in json.loads(result["body"])["detail"]

    def test_invalid_har_400(self):
        result = handler.lambda_handler(
            _event(
                "POST",
                "/api/analyze",
                json.dumps({"kind": "har", "har": {"foo": "bar"}}),
            ),
            None,
        )
        assert result["statusCode"] == 400
        assert "HAR" in json.loads(result["body"])["detail"]

    def test_missing_request_method_returns_400_not_500(self):
        """Regression: chrome.devtools.network entries occasionally arrive
        with no 'method' field on certain Chromium navigation paths. The
        handler must surface that as an actionable 400 with a useful detail
        rather than a silent 500."""
        result = handler.lambda_handler(
            self._har_event(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {"url": "https://x/"},
                                "response": {"status": 401},
                            },
                        ],
                    },
                },
            ),
            None,
        )
        assert result["statusCode"] == 400
        assert "method" in json.loads(result["body"])["detail"]

    def test_out_of_range_response_status_returns_400(self):
        """Regression: Pydantic ValidationError from the engine's model
        construction is a subclass of ValueError and must be caught at the
        handler layer so the user sees a 400 with detail, not a 500."""
        result = handler.lambda_handler(
            self._har_event(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://x/",
                                },
                                "response": {"status": -1},
                            },
                        ],
                    },
                },
            ),
            None,
        )
        assert result["statusCode"] == 400
        assert "status" in json.loads(result["body"])["detail"]


class TestAnalyzeCurl:
    def test_valid_curl_returns_report(self):
        body = json.dumps(
            {
                "kind": "curl",
                "curl": "curl https://api.example.com/v1/health",
            }
        )
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 200
        report = json.loads(result["body"])
        assert report["source"] == "curl"
        assert report["request"]["method"] == "GET"
        assert report["response"] is None  # curl parser doesn't execute

    def test_missing_curl_field_400(self):
        result = handler.lambda_handler(
            _event("POST", "/api/analyze", json.dumps({"kind": "curl"})), None
        )
        assert result["statusCode"] == 400
        assert "Missing 'curl'" in json.loads(result["body"])["detail"]

    def test_rejected_curl_writes_nothing_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Lambda sends stderr to CloudWatch; argparse would print the
        unrecognized --cookie and its value there."""
        curl = "curl https://api.example.com/ --cookie 'session=SECRET-7f3a'"
        body = json.dumps({"kind": "curl", "curl": curl})
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 400
        assert capsys.readouterr().err == ""

    def test_curl_over_the_limit_400_without_parsing_it(self) -> None:
        """A command over parser.MAX_CURL_CHARS (50,000) is refused before
        uncurl splits it, which slows down quadratically; one of 1,000,000
        characters took over 10 seconds and could hold the Lambda."""
        curl = "curl https://api.example.com/ -d '" + "a" * 49_966 + "'"
        assert len(curl) == 50_001
        body = json.dumps({"kind": "curl", "curl": curl})
        started = time.perf_counter()
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        elapsed = time.perf_counter() - started
        assert result["statusCode"] == 400
        assert elapsed < 1
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert json.loads(result["body"])["detail"] == str(e.value)

    def test_curl_over_the_argument_limit_400(self) -> None:
        """A command over parser.MAX_CURL_ARGS (1,000) is refused before
        uncurl's argparse reads it, which slows down with the square of the
        number of options; 100,000 characters of -k flags took 21 seconds."""
        curl = "curl https://api.example.com/" + " -k" * 999
        assert len(shlex.split(curl)) == 1_001
        body = json.dumps({"kind": "curl", "curl": curl})
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 400
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert json.loads(result["body"])["detail"] == str(e.value)

    def test_curl_cookie_header_over_the_limit_400(self) -> None:
        """A Cookie header value over parser.MAX_CURL_COOKIE_CHARS (4,096) is
        refused before uncurl reads it with SimpleCookie, which slows down with
        the square of its length; a 40,000-character command whose Cookie value
        is a=a=..." took 18 seconds locally."""
        curl = "curl https://api.example.com/ -H 'Cookie:" + "=" * 4_096 + "\U0001f600'"
        assert len(shlex.split(curl)[3]) == len("Cookie:") + 4_097
        body = json.dumps({"kind": "curl", "curl": curl})
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 400
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert json.loads(result["body"])["detail"] == str(e.value)


def _jwt(payload: bytes) -> str:
    """A JWT whose payload segment is `payload` exactly."""
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{b64}.sig"


def _bearer_findings(token: str) -> list[dict[str, Any]]:
    """Findings for a curl command that sends `token` as its Bearer token."""
    curl = f"curl https://api.example.com/ -H 'Authorization: Bearer {token}'"
    body = json.dumps({"kind": "curl", "curl": curl})
    result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
    assert result["statusCode"] == 200, result["body"]
    findings: list[dict[str, Any]] = json.loads(result["body"])["findings"]
    return findings


class TestAnalyzeJwtTimeClaims:
    @staticmethod
    def _findings(payload: str) -> list[dict[str, Any]]:
        """Findings for a curl command whose Bearer JWT payload is `payload`
        as written."""
        return _bearer_findings(_jwt(payload.encode("utf-8")))

    def test_expired_token_is_reported(self) -> None:
        findings = self._findings(f'{{"sub": "u", "exp": {int(time.time()) - 7200}}}')
        assert "auth.jwt.expired" in [f["id"] for f in findings]

    def test_not_yet_valid_token_is_reported(self) -> None:
        findings = self._findings(f'{{"sub": "u", "nbf": {int(time.time()) + 1800}}}')
        assert "auth.jwt.not_yet_valid" in [f["id"] for f in findings]

    @pytest.mark.parametrize("claim", ["exp", "nbf"])
    @pytest.mark.parametrize(
        "literal",
        ["NaN", "Infinity", "-Infinity", "-1e300", "1e20", "253402300800", "-62135596801"],
    )
    def test_out_of_range_claim_gives_the_report_of_a_token_without_it(
        self, claim: str, literal: str
    ) -> None:
        with_claim = self._findings(f'{{"sub": "u", "{claim}": {literal}}}')
        assert with_claim == self._findings('{"sub": "u"}')


class TestAnalyzeMalformedJwtPayload:
    # 15,000 deep is past json's recursion limit (under 10,000 levels on
    # Python 3.12) and keeps the curl command under parser.MAX_CURL_CHARS.
    @pytest.mark.parametrize(
        "payload",
        [b"\xff", b'{"exp": ' + b"9" * 4301 + b"}", b"[" * 15_000 + b"]" * 15_000],
        ids=["not-utf-8", "4301-digit-int", "nested-15000-deep"],
    )
    def test_gives_the_report_of_a_token_that_is_not_a_jwt(self, payload: bytes) -> None:
        assert _bearer_findings(_jwt(payload)) == _bearer_findings("not-a-jwt")


_UNENCODABLE = "input holds text that can't be encoded as JSON"

# Payload {"sub":"\ud800","exp":0} as written: JSON's escape for an
# unpaired surrogate. The expired-token finding copies sub into evidence.
_SURROGATE_SUB_JWT = _jwt(b'{"sub":"\\ud800","exp":0}')


class TestUnencodableText:
    """JSON input can carry an unpaired surrogate (\\ud800), which UTF-8
    can't encode. A report that would hold one gets a 400 saying so; these
    used to be a 500."""

    @pytest.mark.parametrize(
        "header",
        [f"Authorization: Bearer {_SURROGATE_SUB_JWT}", "X-Note: a\ud800b"],
        ids=["jwt-sub", "header-value"],
    )
    def test_analyze_returns_400(self, header: str) -> None:
        curl = f"curl https://api.example.com/ -H '{header}'"
        body = json.dumps({"kind": "curl", "curl": curl})
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"detail": _UNENCODABLE}

    def test_run_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def run_offline(**kwargs: Any) -> CapturedRequest:
            kwargs.update(
                transport=httpx.MockTransport(lambda request: httpx.Response(401)),
                probe_network=False,
            )
            return run(**kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(handler, "run_request", run_offline)
        body = json.dumps(
            {
                "method": "GET",
                "url": "https://api.example.com/",
                "headers": {"Authorization": f"Bearer {_SURROGATE_SUB_JWT}"},
            }
        )
        result = handler.lambda_handler(_event("POST", "/api/run", body), None)
        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"detail": _UNENCODABLE}

    def test_other_serialization_errors_stay_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unserializable(captured: CapturedRequest) -> Report:
            report = analyze(captured)
            finding = Finding(
                id="auth.jwt.expired",
                severity="critical",
                title="Expired",
                explanation="...",
                evidence={"exp": object()},
            )
            return report.model_copy(update={"findings": [finding]})

        monkeypatch.setattr(handler, "analyze", unserializable)
        body = json.dumps({"kind": "curl", "curl": "curl https://api.example.com/"})
        result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)
        assert result["statusCode"] == 500
        assert json.loads(result["body"]) == {"detail": "Internal error."}


def _har_response_findings(
    status: int, headers: dict[str, str], body: str | None = None
) -> list[dict[str, Any]]:
    """Findings /api/analyze gives for a HAR whose response has `status`,
    `headers` and, if given, the JSON-typed body `body`."""
    response: dict[str, Any] = {
        "status": status,
        "statusText": "",
        "headers": [{"name": k, "value": v} for k, v in headers.items()],
        "httpVersion": "HTTP/1.1",
    }
    if body is not None:
        response["content"] = {"mimeType": "application/json", "text": body}
    entry = {
        "request": {"method": "GET", "url": "https://api.example.com/", "headers": []},
        "response": response,
        "timings": {},
    }
    har = {"log": {"version": "1.2", "entries": [entry]}}
    result = handler.lambda_handler(
        _event("POST", "/api/analyze", json.dumps({"kind": "har", "har": har})), None
    )
    assert result["statusCode"] == 200, result["body"]
    findings: list[dict[str, Any]] = json.loads(result["body"])["findings"]
    return findings


_RATE_LIMIT_HEADERS = {
    "Retry-After": "60",
    "X-RateLimit-Limit": "1000",
    "X-RateLimit-Remaining": "5",
    "X-RateLimit-Reset": "1761399420",
}


class TestAnalyzeUnparseableRateLimitValues:
    """A rate-limit header no int comes from gives the report of a response
    without it. These used to be a 400 carrying Python's error message."""

    @pytest.mark.parametrize(
        "value", ["9" * 4301, "\u00b2"], ids=["4301-digits", "superscript-two"]
    )
    @pytest.mark.parametrize("name", list(_RATE_LIMIT_HEADERS))
    @pytest.mark.parametrize("status", [429, 200])
    def test_gives_the_findings_without_it(self, status: int, name: str, value: str) -> None:
        without = {k: v for k, v in _RATE_LIMIT_HEADERS.items() if k != name}
        with_it = _har_response_findings(status, without | {name: value})
        assert with_it == _har_response_findings(status, without)


# What the parser says about a NUL right after "{", once the body is read as
# UTF-8.
_NUL_PARSE_ERROR = "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"


class TestMalformedJsonBodiesPythonCantRead:
    """JSON-typed bodies get a report: the check reads them as UTF-8 only,
    parses integers without converting them, and skips nesting past the
    recursion limit. Bodies json.loads raised on with something other than
    JSONDecodeError used to be a 400 carrying Python's message or a 500."""

    @pytest.mark.parametrize(
        "body",
        ["9" * 4301, "[" * 100_000 + "]" * 100_000],
        ids=["4301-digit-int", "nested-100000-deep"],
    )
    def test_valid_json_past_python_limits_gives_the_findings_of_valid_json(
        self, body: str
    ) -> None:
        with_it = _har_response_findings(200, {"Content-Type": "application/json"}, body)
        assert with_it == _har_response_findings(200, {"Content-Type": "application/json"}, "[]")

    @pytest.mark.parametrize(
        ("body", "parse_error"),
        [
            ("{\x00}\x00 ", _NUL_PARSE_ERROR),
            ("{\x00}\x00", _NUL_PARSE_ERROR),
            ("9" * 4301 + "x", "Extra data: line 1 column 4302 (char 4301)"),
        ],
        ids=["odd-length-nul", "utf-16-le-nul", "long-int-then-junk"],
    )
    def test_analyze_body_that_fails_to_parse_gets_malformed_json(
        self, body: str, parse_error: str
    ) -> None:
        # A HAR's text reaches the check as UTF-8. NUL bytes used to make
        # json.loads read it as UTF-16 (the first failing to decode, the
        # second accepted as "{}"), and int() raised on the long integer
        # before the parser saw the "x".
        findings = _har_response_findings(200, {"Content-Type": "application/json"}, body)
        assert [f["id"] for f in findings] == ["body.malformed_json"]
        assert findings[0]["evidence"]["parse_error"] == parse_error

    def test_analyze_deep_nesting_is_skipped_even_when_malformed(self) -> None:
        with_it = _har_response_findings(200, {"Content-Type": "application/json"}, "[" * 100_000)
        assert with_it == _har_response_findings(200, {"Content-Type": "application/json"}, "[]")

    @pytest.mark.parametrize(
        "content",
        [b'{"name": "caf\xe9"}', "{}".encode("utf-16")],
        ids=["latin-1", "utf-16-with-bom"],
    )
    def test_run_body_that_is_not_utf_8_gets_malformed_json(
        self, monkeypatch: pytest.MonkeyPatch, content: bytes
    ) -> None:
        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def not_utf_8(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Type": "application/json"}, content=content
            )

        def run_offline(**kwargs: Any) -> CapturedRequest:
            kwargs.update(transport=httpx.MockTransport(not_utf_8), probe_network=False)
            return run(**kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(handler, "run_request", run_offline)
        body = json.dumps({"method": "GET", "url": "https://api.example.com/"})
        result = handler.lambda_handler(_event("POST", "/api/run", body), None)
        assert result["statusCode"] == 200, result["body"]
        findings = json.loads(result["body"])["findings"]
        assert "body.malformed_json" in [f["id"] for f in findings]


class TestAnalyzeHarParseErrorQuotingASurrogate:
    """parse_har's error quotes the entry's URL, unpaired surrogate and all;
    json.dumps escapes it, so the handler sends that message. api-medic
    serve can't render it and answers with the unencodable-text message."""

    @pytest.mark.parametrize(
        ("request_", "detail"),
        [
            (
                {"url": "https://x.com/\ud800"},
                "HAR entry[0] (https://x.com/\ud800): request.method is missing.",
            ),
            (
                {"method": 1, "url": "https://x.com/\ud800"},
                "HAR entry[0] (https://x.com/\ud800): request.method must be a string, got int.",
            ),
        ],
        ids=["no-method", "method-int"],
    )
    def test_returns_400_with_the_parse_error(self, request_: dict[str, Any], detail: str) -> None:
        payload = {"kind": "har", "har": {"log": {"entries": [{"request": request_}]}}}
        result = handler.lambda_handler(_event("POST", "/api/analyze", json.dumps(payload)), None)
        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"detail": detail}


class TestAnalyzeRejectsInputThatCantMakeAReport:
    """analyze() can't make a Report of a URL or method holding an unpaired
    surrogate (RequestSummary can't validate it); the handler answers 400
    with the error, which api-medic serve now matches."""

    @pytest.mark.parametrize(
        "payload",
        [
            {
                "kind": "har",
                "har": {
                    "log": {
                        "entries": [{"request": {"method": "GET", "url": "https://x.com/\ud800"}}]
                    }
                },
            },
            {
                "kind": "har",
                "har": {
                    "log": {
                        "entries": [{"request": {"method": "G\ud800T", "url": "https://x.com/"}}]
                    }
                },
            },
            {"kind": "curl", "curl": "curl 'https://x.com/\ud800'"},
        ],
        ids=["har-url", "har-method", "curl-url"],
    )
    def test_returns_400_with_the_error(self, payload: dict[str, Any]) -> None:
        if payload["kind"] == "har":
            captured = parse_har(payload["har"])
        else:
            captured = parse_curl(payload["curl"])
        with pytest.raises(ValueError) as e:
            analyze(captured)
        result = handler.lambda_handler(_event("POST", "/api/analyze", json.dumps(payload)), None)
        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"detail": str(e.value)}


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


class TestAnalyzeUnencodableBodyText:
    @pytest.mark.parametrize("payload", _UNENCODABLE_BODY_TEXT, ids=_UNENCODABLE_BODY_TEXT_IDS)
    def test_returns_400(self, payload: dict[str, Any]) -> None:
        result = handler.lambda_handler(_event("POST", "/api/analyze", json.dumps(payload)), None)
        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"detail": _UNENCODABLE}


# Remaining with 309+ more digits than Limit: remaining / limit is too large
# for a float and raised OverflowError, which nothing caught.
_HUGE_REMAINING = ["1" + "0" * 400, "9" * 4300]
_HUGE_REMAINING_IDS = ["1e400", "4300-nines"]


class TestAnalyzeApproachingRatioTooLargeForAFloat:
    """These used to be a 500 "Internal error."."""

    @pytest.mark.parametrize("remaining", _HUGE_REMAINING, ids=_HUGE_REMAINING_IDS)
    def test_gives_the_findings_of_a_ratio_of_one(self, remaining: str) -> None:
        with_it = _har_response_findings(
            200, {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": remaining}
        )
        assert with_it == _har_response_findings(
            200, {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "1"}
        )


class TestAnalyzeRouting:
    def test_unknown_kind_400(self):
        result = handler.lambda_handler(
            _event("POST", "/api/analyze", json.dumps({"kind": "raw"})), None
        )
        assert result["statusCode"] == 400
        assert "Unknown kind" in json.loads(result["body"])["detail"]

    def test_missing_kind_400(self):
        result = handler.lambda_handler(
            _event("POST", "/api/analyze", json.dumps({"har": {}})), None
        )
        assert result["statusCode"] == 400

    def test_garbage_body_400(self):
        result = handler.lambda_handler(_event("POST", "/api/analyze", "{not valid json"), None)
        assert result["statusCode"] == 400
        assert "valid JSON" in json.loads(result["body"])["detail"]

    def test_base64_encoded_body_decoded(self):
        body = json.dumps({"kind": "curl", "curl": "curl https://x.com/"}).encode("utf-8")
        encoded = base64.b64encode(body).decode("ascii")
        result = handler.lambda_handler(
            _event("POST", "/api/analyze", encoded, is_base64=True), None
        )
        assert result["statusCode"] == 200


class TestUnhandledErrors:
    def test_logged_without_request_data(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unexpected exception becomes a plain 500, and the log names only
        its type and location: its message can quote request data."""
        secret = "SECRET-7f3a"

        def boom(captured: CapturedRequest) -> Any:
            raise RuntimeError(f"cannot analyze https://api.example.com/?token={secret}")

        monkeypatch.setattr(handler, "analyze", boom)
        curl = f"curl 'https://api.example.com/?token={secret}'"
        body = json.dumps({"kind": "curl", "curl": curl})
        with caplog.at_level(logging.DEBUG):
            result = handler.lambda_handler(_event("POST", "/api/analyze", body), None)

        assert result["statusCode"] == 500
        assert json.loads(result["body"]) == {"detail": "Internal error."}
        out, err = capsys.readouterr()
        assert secret not in caplog.text + out + err
        assert "Unhandled RuntimeError at " in caplog.text


class TestRouting:
    def test_unknown_path_404(self):
        result = handler.lambda_handler(_event("GET", "/api/whatever"), None)
        assert result["statusCode"] == 404

    def test_wrong_method_404(self):
        result = handler.lambda_handler(_event("PATCH", "/api/health"), None)
        assert result["statusCode"] == 404

    def test_options_preflight_ok(self):
        result = handler.lambda_handler(_event("OPTIONS", "/api/analyze"), None)
        assert result["statusCode"] == 200


Routes = Callable[[httpx.Request], httpx.Response]

# Names (as the resolver sees them, after socket's IDNA 2003 encoding) that
# resolve to a private address in TestRunUrlGuard; everything else is public.
_PRIVATE_NAMES = {"internal.example.com", "xn--x-qfa.example"}


class TestRunUrlGuard:
    """/api/run must vet every URL it fetches with the SSRF guard: the
    submitted URL as httpx will send it, and each redirect hop before it is
    fetched. MockTransport stands in for the network and records what was
    actually requested; getaddrinfo is stubbed so the guard's DNS step
    stays offline."""

    @pytest.fixture(autouse=True)
    def _stub_dns(self, monkeypatch):
        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            # The real call IDNA-encodes str hosts first (and raises
            # UnicodeError for labels it can't encode); mirror that.
            name = host.encode("idna").decode("ascii")
            ip = "10.0.0.5" if name in _PRIVATE_NAMES else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    def _post_run(
        self, monkeypatch: pytest.MonkeyPatch, url: str, routes: Routes
    ) -> tuple[dict[str, Any], list[str], list[CapturedRequest]]:
        fetched: list[str] = []
        captured: list[CapturedRequest] = []

        def record(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            return routes(request)

        real_run = run

        def run_offline(**kwargs: Any) -> CapturedRequest:
            kwargs.update(transport=httpx.MockTransport(record), probe_network=False)
            cap: CapturedRequest = real_run(**kwargs)
            captured.append(cap)
            return cap

        monkeypatch.setattr(handler, "run_request", run_offline)
        body = json.dumps({"method": "GET", "url": url})
        result = handler.lambda_handler(_event("POST", "/api/run", body), None)
        return result, fetched, captured

    @staticmethod
    def _redirect_to(location: str) -> Routes:
        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": location})
            return httpx.Response(200, content=b"internal")

        return routes

    def test_redirect_to_loopback_is_blocked_and_never_fetched(self, monkeypatch):
        result, fetched, _ = self._post_run(
            monkeypatch,
            "https://api.example.com/start",
            self._redirect_to("http://127.0.0.1/admin"),
        )
        assert result["statusCode"] == 400
        assert "http://127.0.0.1/admin" in json.loads(result["body"])["detail"]
        assert fetched == ["https://api.example.com/start"]

    def test_redirect_to_plain_http_public_url_is_blocked(self, monkeypatch):
        result, fetched, _ = self._post_run(
            monkeypatch,
            "https://api.example.com/start",
            self._redirect_to("http://example.org/landing"),
        )
        assert result["statusCode"] == 400
        detail = json.loads(result["body"])["detail"]
        assert "http://example.org/landing" in detail
        assert "Only https" in detail
        assert fetched == ["https://api.example.com/start"]

    @pytest.mark.parametrize(
        "location",
        [
            "https://127.0.0.1/",  # loopback, https
            "https://169.254.169.254/latest/meta-data/",  # EC2 metadata
            "https://[::1]/",  # IPv6 loopback
            "https://100.64.0.1/",  # CGNAT
            "https://internal.example.com/",  # hostname resolving to 10.0.0.5
            "https://" + "a" * 64 + ".example/",  # label getaddrinfo can't encode
        ],
    )
    def test_https_hop_to_unsafe_target_is_blocked(self, monkeypatch, location):
        result, fetched, _ = self._post_run(
            monkeypatch, "https://api.example.com/start", self._redirect_to(location)
        )
        assert result["statusCode"] == 400
        assert location in json.loads(result["body"])["detail"]
        assert fetched == ["https://api.example.com/start"]

    def test_idna_hostname_is_checked_as_httpx_sends_it(self, monkeypatch):
        """socket encodes 'xß' with IDNA 2003 ('xss'); httpx sends IDNA 2008
        ('xn--x-qfa'). The guard must vet the name httpx connects to."""
        result, fetched, _ = self._post_run(
            monkeypatch, "https://xß.example/start", lambda r: httpx.Response(200)
        )
        assert result["statusCode"] == 400
        assert "10.0.0.5" in json.loads(result["body"])["detail"]
        assert fetched == []

    def test_idna_hostname_is_checked_as_typed(self, monkeypatch):
        """The TLS probe connects by the name as typed, which socket encodes
        with IDNA 2003 ('xß' -> 'xss'), not as httpx sends it ('xn--x-qfa').
        The guard must vet that name too."""
        monkeypatch.setattr(sys.modules[__name__], "_PRIVATE_NAMES", {"xss.example"})
        result, fetched, _ = self._post_run(
            monkeypatch, "https://xß.example/start", lambda r: httpx.Response(200)
        )
        assert result["statusCode"] == 400
        assert "10.0.0.5" in json.loads(result["body"])["detail"]
        assert fetched == []

    def test_url_httpx_rejects_returns_400(self, monkeypatch):
        result, fetched, _ = self._post_run(
            monkeypatch, "https://api.example.com:abc/", lambda r: httpx.Response(200)
        )
        assert result["statusCode"] == 400
        assert "Invalid URL" in json.loads(result["body"])["detail"]
        assert fetched == []

    @pytest.mark.parametrize("location", ["https://xn--zz.example/", "https:example.com"])
    def test_unusable_location_gives_a_report_without_response(self, monkeypatch, location):
        """httpx builds the next request as soon as a redirect arrives and
        raises for a Location it can't use. That's the target server's
        doing, so it fails the exchange (no response) instead of a 500."""
        result, fetched, _ = self._post_run(
            monkeypatch, "https://api.example.com/start", self._redirect_to(location)
        )
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["response"] is None
        assert fetched == ["https://api.example.com/start"]

    def test_hop_with_out_of_range_port_is_blocked(self, monkeypatch):
        result, fetched, _ = self._post_run(
            monkeypatch,
            "https://api.example.com/start",
            self._redirect_to("https://example.com:99999/"),
        )
        assert result["statusCode"] == 400
        detail = json.loads(result["body"])["detail"]
        assert "Blocked redirect to https://example.com:99999/" in detail
        assert "port must be a whole number from 1 to 65535" in detail
        assert fetched == ["https://api.example.com/start"]

    def test_request_url_is_not_logged_by_httpx(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx logs each request's URL at INFO; that must not reach the
        Lambda's logs even if they're configured to keep INFO."""
        with caplog.at_level(logging.INFO):
            result, fetched, _ = self._post_run(
                monkeypatch,
                "https://api.example.com/start?token=SECRET-7f3a",
                lambda r: httpx.Response(200),
            )
        assert result["statusCode"] == 200
        assert fetched == ["https://api.example.com/start?token=SECRET-7f3a"]
        assert "SECRET-7f3a" not in caplog.text

    def test_hop_userinfo_is_not_turned_into_basic_auth(self, monkeypatch):
        """httpx's auto-follow ignores user:pass@ in a Location; a hand-
        followed hop must not turn it into an Authorization header either."""
        seen: dict[str, str | None] = {}

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"Location": "https://user:secret@api.example.com/next"}
                )
            seen["authorization"] = request.headers.get("Authorization")
            return httpx.Response(200)

        result, fetched, _ = self._post_run(monkeypatch, "https://api.example.com/start", routes)
        assert result["statusCode"] == 200
        assert len(fetched) == 2
        assert seen["authorization"] is None

    def test_chain_longer_than_five_hops_stops_after_five(self, monkeypatch):
        def routes(request: httpx.Request) -> httpx.Response:
            # /step/N → /step/N-1, terminating at /step/0 with a 200.
            n = int(request.url.path.rsplit("/", 1)[-1])
            if n > 0:
                return httpx.Response(302, headers={"Location": f"/step/{n - 1}"})
            return httpx.Response(200, content=b"done")

        result, fetched, _ = self._post_run(monkeypatch, "https://api.example.com/step/7", routes)

        # The original request plus five followed hops; the sixth target
        # (/step/1) is never requested.
        assert fetched == [f"https://api.example.com/step/{n}" for n in range(7, 1, -1)]
        assert result["statusCode"] == 200
        report = json.loads(result["body"])
        assert report["response"]["status_code"] == 302
        too_many = next(f for f in report["findings"] if f["id"] == "http.redirect.too_many")
        assert too_many["evidence"]["redirect_count"] == 5
        assert too_many["evidence"]["chain"] == fetched
        # The Report must not claim a final response was reached.
        assert too_many["title"] == "Redirect chain stopped after 5 hops"
        assert "before reaching a final response" not in too_many["explanation"]
        assert too_many["evidence"]["stopped"] is True
        assert too_many["evidence"]["unfollowed_location"] == "https://api.example.com/step/1"

    @pytest.mark.parametrize("hops", [1, 5])
    def test_https_redirects_produce_same_report_as_unguarded_runner(self, monkeypatch, hops):
        """Within the cap, the guarded path must capture exactly what the
        unguarded (CLI / local web UI) path captures, so the Report — and
        the redirect evidence the redirect checks read — is unchanged."""

        def routes(request: httpx.Request) -> httpx.Response:
            n = int(request.url.path.rsplit("/", 1)[-1])
            if n > 0:
                host = "www.example.com" if n % 2 else "api.example.com"
                return httpx.Response(301, headers={"Location": f"https://{host}/hop/{n - 1}"})
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"ok")

        url = f"https://api.example.com/hop/{hops}"
        result, fetched, captured = self._post_run(monkeypatch, url, routes)
        unguarded = run(
            "GET", url, headers={}, transport=httpx.MockTransport(routes), probe_network=False
        )

        assert result["statusCode"] == 200
        assert len(fetched) == hops + 1
        assert captured[0].redirect_chain == unguarded.redirect_chain == fetched
        assert captured[0].response == unguarded.response

        volatile = ("id", "timestamp", "timing")
        guarded_report = {k: v for k, v in json.loads(result["body"]).items() if k not in volatile}
        expected = analyze(unguarded).model_dump(mode="json")
        assert guarded_report == {k: v for k, v in expected.items() if k not in volatile}


@pytest.fixture(autouse=True)
def _path_cleanup():
    """Each test runs with handler module in sys.modules; that's fine."""
    yield


_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def _redirect(location: str) -> bytes:
    return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()


class _RecordingStream(httpcore.MockStream):
    """MockStream that records the SNI it's given and the bytes sent."""

    def __init__(self, buffer: list[bytes], state: dict[str, Any]) -> None:
        super().__init__(buffer)
        self._state = state

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self._state["sni"].append(server_hostname)
        return self

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._state["sent"].append(buffer)


class TestRunConnectionPinning:
    """Every connection /api/run opens (the TLS probe, the request, each
    redirect hop) must go to an address checked in the same step, never to a
    fresh lookup of the name. The fake resolver answers each name from a
    script, one answer per lookup with the last repeating, so a name can look
    public to the guard and private to the connection, as a DNS-rebinding
    server would arrange. Nothing is opened: the probe's
    socket.create_connection and httpcore's connect_tcp are recorded, and
    connect_tcp answers from canned responses."""

    @pytest.fixture
    def net(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        state: dict[str, Any] = {
            "answers": {},
            "probe": [],
            "tcp": [],
            "responses": [],
            "sni": [],
            "sent": [],
        }

        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            script = state["answers"][host]
            ip = script.pop(0) if len(script) > 1 else script[0]
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

        def create_connection(address: tuple[str, int], *a: Any, **kw: Any) -> Any:
            state["probe"].append(address)
            raise ConnectionRefusedError("offline test")

        def connect_tcp(
            self: httpcore.SyncBackend, host: str, port: int, *a: Any, **kw: Any
        ) -> httpcore.NetworkStream:
            state["tcp"].append((host, port))
            return _RecordingStream([state["responses"].pop(0)], state)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", create_connection)
        monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
        monkeypatch.setattr(
            "api_medic.core.runner._resolve_dns",
            lambda host, timeout=5.0: (CapturedDns(records=[]), 0.0),
        )
        return state

    @staticmethod
    def _post(url: str) -> dict[str, Any]:
        body = json.dumps({"method": "GET", "url": url})
        return handler.lambda_handler(_event("POST", "/api/run", body), None)

    def test_name_that_rebinds_after_the_guard_is_never_connected(
        self, net: dict[str, Any]
    ) -> None:
        # The guard looks the name up twice (as typed, as httpx sends it).
        net["answers"] = {"rebind.example": ["93.184.216.34", "93.184.216.34", "10.0.0.5"]}
        net["responses"] = [_OK]

        result = self._post("https://rebind.example/")

        assert (net["probe"], net["tcp"]) == ([], [])
        assert result["statusCode"] == 400
        assert "10.0.0.5" in json.loads(result["body"])["detail"]

    def test_probe_and_request_connect_to_the_checked_address(
        self, net: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        net["answers"] = {"api.example.com": ["93.184.216.34"]}
        net["responses"] = [_OK]
        probe_sni: list[str | None] = []

        # Let the probe get as far as its TLS handshake, and record the SNI.
        def create_connection(address: tuple[str, int], *a: Any, **kw: Any) -> socket.socket:
            net["probe"].append(address)
            return socket.socket()

        def wrap_socket(
            self: ssl.SSLContext,
            sock: socket.socket,
            *a: Any,
            server_hostname: str | None = None,
            **kw: Any,
        ) -> ssl.SSLSocket:
            probe_sni.append(server_hostname)
            raise ssl.SSLError("offline test")

        monkeypatch.setattr(socket, "create_connection", create_connection)
        monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)

        result = self._post("https://api.example.com/")

        assert result["statusCode"] == 200
        assert net["probe"] == [("93.184.216.34", 443)]
        assert net["tcp"] == [("93.184.216.34", 443)]
        # Connecting to the checked IP leaves SNI and Host on the name.
        assert probe_sni == ["api.example.com"]
        assert net["sni"] == ["api.example.com"]
        assert b"Host: api.example.com\r\n" in b"".join(net["sent"])

    def test_hop_that_rebinds_after_its_check_is_never_connected(self, net: dict[str, Any]) -> None:
        net["answers"] = {
            "api.example.com": ["93.184.216.34"],
            "hop.example": ["93.184.216.35", "169.254.169.254"],
        }
        net["responses"] = [_redirect("https://hop.example/next"), _OK]

        result = self._post("https://api.example.com/start")

        assert result["statusCode"] == 400
        assert "169.254.169.254" in json.loads(result["body"])["detail"]
        assert net["tcp"] == [("93.184.216.34", 443)]

    def test_hop_to_an_ipv6_address_and_port_is_pinned(self, net: dict[str, Any]) -> None:
        v6 = "2606:2800:220:1:248:1893:25c8:1946"
        net["answers"] = {"api.example.com": ["93.184.216.34"], "hop.example": [v6]}
        net["responses"] = [_redirect("https://hop.example:8443/next"), _OK]

        result = self._post("https://api.example.com/start")

        assert result["statusCode"] == 200
        assert net["tcp"] == [("93.184.216.34", 443), (v6, 8443)]

    def test_proxy_environment_is_ignored(
        self, net: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A proxy resolves the name itself, out of reach of the check, so the
        guarded path must connect directly even if the environment sets one."""
        for var in ("https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
        net["answers"] = {"api.example.com": ["93.184.216.34"]}
        net["responses"] = [_OK]

        self._post("https://api.example.com/")

        assert net["tcp"] == [("93.184.216.34", 443)]


_PORT = "port must be a whole number from 1 to 65535"
_BRACKETS = "square brackets are only allowed around an IPv6 address"
_IDNA = "is not a valid internationalized hostname"
_DNS = "can't be looked up in DNS"
_ASCII_HEADERS = "Header names and values must be ASCII"


class TestRunRejectsMalformedInput:
    """Input /api/run can't send gets a 400 naming the problem, before any
    DNS lookup or fetch. Each of these used to be a 500 on this route, on
    api-medic serve's, or both."""

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        seen: dict[str, list[str]] = {"lookups": [], "fetched": []}

        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            seen["lookups"].append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def record(request: httpx.Request) -> httpx.Response:
            seen["fetched"].append(str(request.url))
            return httpx.Response(200)

        real_run = run

        def run_offline(**kwargs: Any) -> CapturedRequest:
            kwargs.update(transport=httpx.MockTransport(record), probe_network=False)
            return real_run(**kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(handler, "run_request", run_offline)
        return seen

    @staticmethod
    def _post(fields: dict[str, Any]) -> tuple[int, str]:
        body = json.dumps({"method": "GET", "url": "https://api.example.com/", **fields})
        result = handler.lambda_handler(_event("POST", "/api/run", body), None)
        return result["statusCode"], json.loads(result["body"]).get("detail", "")

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("https://api.example.com:99999/", _PORT),
            ("https://api.example.com:65536/", _PORT),
            ("https://api.example.com:0/", _PORT),
            ("https://api.example.com:-1/", _PORT),
            ("https://api.example.com:+443/", _PORT),
            ("https://api.example.com: 443/", _PORT),
            ("https://api.example.com:\u0663/", _PORT),
            ("https://api.example.com:1_0/", _PORT),
            ("https://api.example.com:443:443/", _PORT),
            ("https://api.example.com:abc/", _PORT),
            ("https://[2001:db8::1]:99999/", _PORT),
            # Over int()'s 4,300-digit limit, leading zeros included.
            ("https://api.example.com:" + "9" * 4301 + "/", _PORT),
            ("https://api.example.com:" + "0" * 4301 + "/", _PORT),
            ("https://api.example.com:" + "0" * 4300 + "443/", "Invalid port"),
            ("https://[fe80::1%25\u00e9]/", "the host contains non-ASCII characters"),
            ("https://[example.com]/", _BRACKETS),
            ("https://[1.2.3.4]/", _BRACKETS),
            ("https://[]/", _BRACKETS),
            ("https://[::1/", _BRACKETS),
            ("https://::1]/", _BRACKETS),
            ("https://ex[ample].com/", _BRACKETS),
            ("https://[2001:db8::1]x/", _BRACKETS),
            ("https://user@[example.com]/", _BRACKETS),
            ("https://[v1.fe]/", _BRACKETS),
            ("https://xn--zz.example/", _IDNA),
            ("https://xn--/", _IDNA),
            ("https://xn--a.example/", _IDNA),
            ("https://xn--abc-.example/", _IDNA),
            ("https://xn--ls8h.la/", _IDNA),
            ("https://xn--n3h.net/", _IDNA),
            ("https://" + "a" * 64 + ".example/", _DNS),
            ("https://a..b.example/", _DNS),
            ("https://exa\tmple.com/", "Invalid URL"),
            ("https://256.1.1.1/", "Invalid URL"),
            ("https://example.com/\ud800", "can't be encoded as UTF-8"),
        ],
    )
    def test_malformed_url(self, seen: dict[str, list[str]], url: str, message: str) -> None:
        status, detail = self._post({"url": url})
        assert status == 400
        assert message in detail
        assert seen == {"lookups": [], "fetched": []}

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"body": 5}, "'body' must be a string or null"),
            ({"body": {"a": 1}}, "'body' must be a string or null"),
            ({"body": ["password=secret"]}, "'body' must be a string or null"),
            ({"body": True}, "'body' must be a string or null"),
            ({"body": "\ud800"}, "Body can't be encoded as UTF-8"),
            ({"headers": {"X-T\u00f6ken": "v"}}, _ASCII_HEADERS),
            ({"headers": {"X-A": "caf\u00e9"}}, _ASCII_HEADERS),
            ({"headers": {"X-A": "\ud800"}}, _ASCII_HEADERS),
            ({"method": "G\u00c9T"}, "Method must be ASCII"),
        ],
    )
    def test_request_httpx_cannot_send(
        self, seen: dict[str, list[str]], fields: dict[str, Any], message: str
    ) -> None:
        status, detail = self._post(fields)
        assert status == 400
        assert message in detail
        assert seen == {"lookups": [], "fetched": []}
