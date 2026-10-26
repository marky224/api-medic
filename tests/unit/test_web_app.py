"""Tests for the FastAPI app — uses fastapi.testclient + monkeypatched Runner."""

from __future__ import annotations

import base64
import json
import socket
import time
from typing import Any

import fastapi.routing
import httpx
import pytest
from fastapi.testclient import TestClient

from api_medic import __version__
from api_medic.core import runner
from api_medic.core.captured import CapturedDns, CapturedRequest, CapturedResponse
from api_medic.core.engine import analyze
from api_medic.core.models import Finding, Report, TimingBreakdown
from api_medic.core.parser import parse_curl, parse_har
from api_medic.web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _fake_captured(**overrides: Any) -> CapturedRequest:
    base = {
        "method": "GET",
        "url": "https://api.example.com/v1/users",
        "headers": {"Accept": "application/json"},
        "body": b"",
        "response": CapturedResponse(
            status_code=200,
            status_text="OK",
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
            protocol="HTTP/2",
        ),
        "timing": TimingBreakdown(total_ms=120.0),
        "source": "live",
    }
    return CapturedRequest(**(base | overrides))


class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == __version__


class TestRunEndpoint:
    def test_executes_runner_and_returns_report(self, client, monkeypatch):
        called: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> CapturedRequest:
            called.update(kwargs)
            return _fake_captured()

        monkeypatch.setattr("api_medic.web.app.run_request", fake_run)

        r = client.post(
            "/api/run",
            json={
                "method": "GET",
                "url": "https://api.example.com/v1/users",
                "headers": {"Accept": "application/json"},
            },
        )
        assert r.status_code == 200
        report = r.json()
        assert report["source"] == "live"
        assert report["request"]["method"] == "GET"
        assert report["response"]["status_code"] == 200
        assert called["method"] == "GET"
        assert called["url"] == "https://api.example.com/v1/users"

    def test_rejects_missing_url(self, client):
        r = client.post("/api/run", json={"method": "GET"})
        assert r.status_code == 422


class TestRunRejectsMalformedInput:
    """Input the runner can't send gets a 400 naming the problem, before any
    DNS lookup, probe or fetch. Each of these used to be a 500 here."""

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        seen: dict[str, list[str]] = {"lookups": [], "probes": [], "fetched": []}

        def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
            seen["lookups"].append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def record(request: httpx.Request) -> httpx.Response:
            seen["fetched"].append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://xn--zz.example/"})
            return httpx.Response(200)

        def run_offline(**kwargs: Any) -> CapturedRequest:
            return runner.run(**kwargs, transport=httpx.MockTransport(record))

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

        def resolve_dns(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
            seen["probes"].append(host)
            return CapturedDns(records=[]), 0.0

        def probe_tls(host: str, port: int, timeout: float = 5.0) -> tuple[None, float]:
            seen["probes"].append(host)
            return None, 0.0

        monkeypatch.setattr(runner, "_resolve_dns", resolve_dns)
        monkeypatch.setattr(runner, "_probe_tls", probe_tls)
        monkeypatch.setattr("api_medic.web.app.run_request", run_offline)
        return seen

    @staticmethod
    def _post(client: TestClient, fields: dict[str, Any]) -> httpx.Response:
        # json.dumps escapes lone surrogates; TestClient's json= can't encode them.
        payload = {"method": "GET", "url": "https://api.example.com/", **fields}
        response: httpx.Response = client.post(
            "/api/run", content=json.dumps(payload), headers={"content-type": "application/json"}
        )
        return response

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"url": "https://api.example.com:99999/"}, "port must be a whole number"),
            ({"url": "https://api.example.com:+443/"}, "port must be a whole number"),
            ({"url": "https://api.example.com:" + "9" * 4301 + "/"}, "port must be a whole number"),
            ({"url": "https://api.example.com:" + "0" * 4301 + "/"}, "port must be a whole number"),
            ({"url": "https://api.example.com:" + "0" * 4300 + "443/"}, "Invalid port"),
            ({"url": "https://[fe80::1%25\u00e9]/"}, "the host contains non-ASCII characters"),
            ({"url": "https://[example.com]/"}, "square brackets are only allowed"),
            ({"url": "https://[::1/"}, "square brackets are only allowed"),
            ({"url": "https://xn--zz.example/"}, "is not a valid internationalized hostname"),
            ({"url": "https://xn--ls8h.la/"}, "is not a valid internationalized hostname"),
            ({"url": "https://" + "a" * 64 + ".example/"}, "can't be looked up in DNS"),
            ({"url": "https://exa\tmple.com/"}, "Invalid URL"),
            ({"url": "https://\u2603.net/"}, "Invalid URL"),
            ({"url": "https://example.com/\ud800"}, "can't be encoded as UTF-8"),
            ({"headers": {"X-T\u00f6ken": "v"}}, "Header names and values must be ASCII"),
            ({"headers": {"X-A": "caf\u00e9"}}, "Header names and values must be ASCII"),
            ({"method": "G\u00c9T"}, "Method must be ASCII"),
            ({"body": "\ud800"}, "Body can't be encoded as UTF-8"),
        ],
    )
    def test_returns_400(
        self,
        client: TestClient,
        seen: dict[str, list[str]],
        fields: dict[str, Any],
        message: str,
    ) -> None:
        r = self._post(client, fields)
        assert r.status_code == 400
        assert message in r.json()["detail"]
        assert seen == {"lookups": [], "probes": [], "fetched": []}

    def test_hop_that_fails_validate_url_is_not_sent(
        self, client: TestClient, seen: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx doesn't range-check a Location's port, and getaddrinfo would
        wrap 99999 to 34463, so the hop is refused and nothing is fetched."""

        def routes(request: httpx.Request) -> httpx.Response:
            seen["fetched"].append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://example.com:99999/"})
            return httpx.Response(200)

        def run_offline(**kwargs: Any) -> CapturedRequest:
            return runner.run(**kwargs, transport=httpx.MockTransport(routes))

        monkeypatch.setattr("api_medic.web.app.run_request", run_offline)
        r = self._post(client, {"url": "https://api.example.com/start"})
        assert r.status_code == 200
        assert r.json()["response"] is None
        assert seen["fetched"] == ["https://api.example.com/start"]

    def test_unusable_location_gives_a_report_without_response(
        self, client: TestClient, seen: dict[str, list[str]]
    ) -> None:
        r = self._post(client, {"url": "https://api.example.com/start"})
        assert r.status_code == 200
        assert r.json()["response"] is None
        assert seen["fetched"] == ["https://api.example.com/start"]


class TestAnalyzeEndpoint:
    def test_har_input_produces_report(self, client):
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
                            ],
                        },
                        "response": {
                            "status": 401,
                            "statusText": "Unauthorized",
                            "headers": [],
                            "httpVersion": "HTTP/2",
                        },
                        "timings": {},
                    },
                ],
            },
        }
        r = client.post("/api/analyze", json={"kind": "har", "har": har})
        assert r.status_code == 200
        report = r.json()
        assert report["source"] == "har"
        assert report["request"]["method"] == "POST"
        assert report["response"]["status_code"] == 401

    def test_curl_input_produces_report(self, client):
        r = client.post(
            "/api/analyze",
            json={
                "kind": "curl",
                "curl": "curl -X GET 'https://api.example.com/health'",
            },
        )
        assert r.status_code == 200
        report = r.json()
        assert report["source"] == "curl"
        assert report["request"]["method"] == "GET"
        assert report["response"] is None

    def test_curl_over_the_limit_returns_400(self, client: TestClient) -> None:
        # One character over parser.MAX_CURL_CHARS (100,000).
        curl = "curl https://api.example.com/ -d '" + "a" * 99_966 + "'"
        assert len(curl) == 100_001
        started = time.perf_counter()
        r = client.post("/api/analyze", json={"kind": "curl", "curl": curl})
        elapsed = time.perf_counter() - started
        assert r.status_code == 400
        assert elapsed < 1
        with pytest.raises(ValueError) as e:
            parse_curl(curl)
        assert r.json()["detail"] == str(e.value)

    def test_invalid_har_returns_400(self, client):
        r = client.post("/api/analyze", json={"kind": "har", "har": {"foo": "bar"}})
        assert r.status_code == 400
        assert "HAR" in r.json()["detail"]

    def test_invalid_kind_returns_422(self, client):
        r = client.post("/api/analyze", json={"kind": "raw", "raw": "..."})
        assert r.status_code == 422


def _jwt(payload: bytes) -> str:
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{b64}.sig"


_UNENCODABLE = "input holds text that can't be encoded as JSON"

# Payload {"sub":"\ud800","exp":0} as written: JSON's escape for an
# unpaired surrogate. The expired-token finding copies sub into evidence.
_SURROGATE_SUB_JWT = _jwt(b'{"sub":"\\ud800","exp":0}')


def _post(client: TestClient, path: str, payload: dict[str, Any]) -> httpx.Response:
    # json.dumps escapes a surrogate as \ud800; httpx's json= would try to
    # encode it as UTF-8 and fail in the test itself.
    response: httpx.Response = client.post(
        path, content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )
    return response


@pytest.fixture(params=["dump_json", "model_dump"])
def serialization(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each way FastAPI serializes a returned Report. Today's FastAPI uses
    pydantic's dump_json, which raises PydanticSerializationError on a
    surrogate. Older releases pyproject allows use model_dump(mode="json")
    and JSONResponse, which raises UnicodeEncodeError. Today's takes that
    path too when the route handler's isinstance(response_class,
    DefaultPlaceholder) check fails, which hiding DefaultPlaceholder forces."""
    if request.param == "model_dump":
        monkeypatch.setattr(fastapi.routing, "DefaultPlaceholder", type("NoFastPath", (), {}))


@pytest.mark.usefixtures("serialization")
class TestUnencodableText:
    """JSON input can carry an unpaired surrogate (\\ud800), which UTF-8
    can't encode. A report that would hold one gets a 400 saying so. These
    used to be a 500, and on the model_dump path still were."""

    def test_report_without_a_surrogate_is_sent(self, client: TestClient) -> None:
        r = _post(
            client, "/api/analyze", {"kind": "curl", "curl": "curl https://x.com/ -H 'X-A: b'"}
        )
        assert r.status_code == 200
        assert r.json()["request"]["headers"] == {"X-A": "b"}

    @pytest.mark.parametrize(
        "header",
        [f"Authorization: Bearer {_SURROGATE_SUB_JWT}", "X-Note: a\ud800b"],
        ids=["jwt-sub", "header-value"],
    )
    def test_analyze_returns_400(self, client: TestClient, header: str) -> None:
        curl = f"curl https://api.example.com/ -H '{header}'"
        r = _post(client, "/api/analyze", {"kind": "curl", "curl": curl})
        assert r.status_code == 400
        assert r.json() == {"detail": _UNENCODABLE}

    def test_run_returns_400(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        headers = {"Authorization": f"Bearer {_SURROGATE_SUB_JWT}"}
        monkeypatch.setattr(
            "api_medic.web.app.run_request", lambda **kw: _fake_captured(headers=headers)
        )
        r = _post(
            client,
            "/api/run",
            {"method": "GET", "url": "https://api.example.com/", "headers": headers},
        )
        assert r.status_code == 400
        assert r.json() == {"detail": _UNENCODABLE}

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

        monkeypatch.setattr("api_medic.web.app.analyze", unserializable)
        client = TestClient(app, raise_server_exceptions=False)
        r = _post(client, "/api/analyze", {"kind": "curl", "curl": "curl https://x.com/"})
        assert r.status_code == 500

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/api/analyze", {"kind": "x\ud800"}),
            ("/api/analyze", {"kind": "har", "har": "\ud800"}),
            ("/api/run", {"method": "GET", "url": "https://x.com/", "headers": "\ud800"}),
            (
                "/api/analyze",
                {
                    "kind": "har",
                    "har": {"log": {"entries": [{"request": {"url": "https://x.com/\ud800"}}]}},
                },
            ),
            (
                "/api/analyze",
                {
                    "kind": "har",
                    "har": {
                        "log": {
                            "entries": [{"request": {"method": 1, "url": "https://x.com/\ud800"}}]
                        }
                    },
                },
            ),
        ],
        ids=["kind", "har-string", "run-headers", "har-url-no-method", "har-url-method-int"],
    )
    def test_error_response_that_would_echo_a_surrogate(
        self, client: TestClient, path: str, payload: dict[str, Any]
    ) -> None:
        """FastAPI's 422 quotes the invalid input, and the 400 for a HAR
        parse error quotes the entry's URL. Rendering either raises the same
        UnicodeEncodeError, which the app-level handler answers. The Lambda
        answers these with its own message instead (see its tests)."""
        r = _post(client, path, payload)
        assert r.status_code == 400
        assert r.json() == {"detail": _UNENCODABLE}

    def test_other_unicode_encode_errors_stay_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run_request(**kwargs: Any) -> CapturedRequest:
            raise UnicodeEncodeError("ascii", "caf\u00e9", 3, 4, "ordinal not in range(128)")

        monkeypatch.setattr("api_medic.web.app.run_request", run_request)
        client = TestClient(app, raise_server_exceptions=False)
        r = _post(client, "/api/run", {"method": "GET", "url": "https://api.example.com/"})
        assert r.status_code == 500


# Input parse_har and parse_curl accept but analyze() can't make a Report of:
# RequestSummary can't validate a str holding an unpaired surrogate.
_UNREPORTABLE: list[dict[str, Any]] = [
    {
        "kind": "har",
        "har": {
            "log": {"entries": [{"request": {"method": "GET", "url": "https://x.com/\ud800"}}]}
        },
    },
    {
        "kind": "har",
        "har": {"log": {"entries": [{"request": {"method": "G\ud800T", "url": "https://x.com/"}}]}},
    },
    {"kind": "curl", "curl": "curl 'https://x.com/\ud800'"},
]
_UNREPORTABLE_IDS = ["har-url", "har-method", "curl-url"]


class TestAnalyzeRejectsInputThatCantMakeAReport:
    """A ValueError from analyze() is a 400 with its message, as on the
    hosted demo. These used to be a 500."""

    @pytest.mark.parametrize("payload", _UNREPORTABLE, ids=_UNREPORTABLE_IDS)
    def test_returns_400_with_the_error(self, client: TestClient, payload: dict[str, Any]) -> None:
        if payload["kind"] == "har":
            captured = parse_har(payload["har"])
        else:
            captured = parse_curl(payload["curl"])
        with pytest.raises(ValueError) as e:
            analyze(captured)
        r = _post(client, "/api/analyze", payload)
        assert r.status_code == 400
        assert r.json() == {"detail": str(e.value)}


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
    def test_returns_400(self, client: TestClient, payload: dict[str, Any]) -> None:
        r = _post(client, "/api/analyze", payload)
        assert r.status_code == 400
        assert r.json() == {"detail": _UNENCODABLE}


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


class TestAnalyzeApproachingRatioTooLargeForAFloat:
    """These used to be a 500."""

    @pytest.mark.parametrize("remaining", _HUGE_REMAINING, ids=_HUGE_REMAINING_IDS)
    def test_gives_the_findings_of_a_ratio_of_one(self, client: TestClient, remaining: str) -> None:
        r = client.post("/api/analyze", json={"kind": "har", "har": _rate_limited_har(remaining)})
        assert r.status_code == 200
        same = client.post("/api/analyze", json={"kind": "har", "har": _rate_limited_har("1")})
        assert r.json()["findings"] == same.json()["findings"]


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


class TestJsonBodiesAreReadAsUtf8:
    """body.malformed_json reads a JSON-typed body as UTF-8 only, parses
    integers without converting them, and skips nesting past the recursion
    limit, in serve as in the Lambda."""

    @pytest.mark.parametrize(("text", "parse_error"), _HAR_JSON_BODIES, ids=_HAR_JSON_BODIES_IDS)
    def test_analyze_har_body(self, client: TestClient, text: str, parse_error: str | None) -> None:
        r = _post(client, "/api/analyze", {"kind": "har", "har": _json_body_har(text)})
        assert r.status_code == 200
        assert _parse_errors(r.json()["findings"]) == ([] if parse_error is None else [parse_error])

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (b'{"name": "caf\xe9"}', "invalid continuation byte"),
            ("{}".encode("utf-16"), "invalid start byte"),
            (b'"\xed\xa0\x80"', "invalid continuation byte"),
        ],
        ids=["latin-1", "utf-16-with-bom", "encoded-surrogate"],
    )
    def test_run_body_that_is_not_utf_8(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, body: bytes, reason: str
    ) -> None:
        # Only a live response can carry these: a HAR's text is UTF-8.
        response = CapturedResponse(
            status_code=200,
            status_text="OK",
            headers={"Content-Type": "application/json"},
            body=body,
            protocol="HTTP/2",
        )
        monkeypatch.setattr(
            "api_medic.web.app.run_request", lambda **kw: _fake_captured(response=response)
        )
        r = client.post("/api/run", json={"method": "GET", "url": "https://api.example.com/"})
        assert r.status_code == 200
        explanations = [
            f["explanation"] for f in r.json()["findings"] if f["id"] == "body.malformed_json"
        ]
        assert len(explanations) == 1
        assert f"can't be decoded as UTF-8 text: {reason}." in explanations[0]


class TestCors:
    def test_preflight_for_vite_origin_succeeds(self, client):
        r = client.options(
            "/api/analyze",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
