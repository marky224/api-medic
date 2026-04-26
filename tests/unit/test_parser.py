"""Tests for core.parser — HAR + curl normalisation into CapturedRequest."""

from __future__ import annotations

import json

import pytest

from api_medic.core.parser import parse_curl, parse_har


def _minimal_har(**overrides):
    entry = {
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

    def test_omits_response_when_missing_or_zero_status(self):
        har = _minimal_har()
        cap = parse_har(har)
        assert cap.response is None

    def test_method_normalized_to_upper(self):
        har = _minimal_har()
        har["log"]["entries"][0]["request"]["method"] = "post"
        cap = parse_har(har)
        assert cap.method == "POST"


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
