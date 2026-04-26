"""Tests for the FastAPI app — uses fastapi.testclient + monkeypatched Runner."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_medic.core.captured import CapturedRequest, CapturedResponse
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
        assert "version" in body


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
