"""Tests for the FastAPI app — uses fastapi.testclient + monkeypatched Runner."""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from api_medic import __version__
from api_medic.core import runner
from api_medic.core.captured import CapturedDns, CapturedRequest, CapturedResponse
from api_medic.core.models import TimingBreakdown
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

    def test_invalid_har_returns_400(self, client):
        r = client.post("/api/analyze", json={"kind": "har", "har": {"foo": "bar"}})
        assert r.status_code == 400
        assert "HAR" in r.json()["detail"]

    def test_invalid_kind_returns_422(self, client):
        r = client.post("/api/analyze", json={"kind": "raw", "raw": "..."})
        assert r.status_code == 422


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
