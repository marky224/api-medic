"""Tests for the AWS Lambda handler under deploy/lambda/handler.py.

handler.py lives outside the api_medic package by architectural choice
(it's a separate deployable unit). Tests put deploy/lambda/ on sys.path
to import it.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

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


@pytest.fixture(autouse=True)
def _path_cleanup():
    """Each test runs with handler module in sys.modules; that's fine."""
    yield
