"""Tests for the AWS Lambda handler under deploy/lambda/handler.py.

handler.py lives outside the api_medic package by architectural choice
(it's a separate deployable unit). Tests put deploy/lambda/ on sys.path
to import it.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from api_medic.core.captured import CapturedRequest
from api_medic.core.engine import analyze
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
        assert "version" in body


class TestAnalyzeHar:
    def _har_event(self, har: dict) -> dict:
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


class TestRunRedirectGuard:
    """/api/run must vet every redirect hop with the SSRF guard before it is
    fetched. MockTransport stands in for the network and records what was
    actually requested; getaddrinfo is stubbed so hostnames resolve to a
    public address and the guard's DNS step stays offline."""

    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )

    def _post_run(
        self, monkeypatch: pytest.MonkeyPatch, url: str, routes: Routes
    ) -> tuple[dict[str, Any], list[str], list[CapturedRequest]]:
        fetched: list[str] = []
        captured: list[CapturedRequest] = []

        def record(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            return routes(request)

        real_run = handler.run_request

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

    def test_redirect_to_cgnat_address_is_blocked(self, monkeypatch):
        result, fetched, _ = self._post_run(
            monkeypatch,
            "https://api.example.com/start",
            self._redirect_to("https://100.64.0.1/"),
        )
        assert result["statusCode"] == 400
        assert "100.64.0.1" in json.loads(result["body"])["detail"]
        assert fetched == ["https://api.example.com/start"]

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
