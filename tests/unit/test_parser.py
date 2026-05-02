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
