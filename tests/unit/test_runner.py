"""Tests for core.runner — live HTTP execution via httpx.

Unit tests use httpx.MockTransport so they don't hit the network. A separate
integration suite (Phase 3b) hits public test endpoints and is gated behind
@pytest.mark.integration.
"""

from __future__ import annotations

import httpx
import pytest

from api_medic.core.runner import DEFAULT_USER_AGENT, run


def _transport(handler):
    return httpx.MockTransport(handler)


class TestRun:
    def test_captures_basic_get_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"hello",
            )

        cap = run("GET", "https://example.com/", transport=_transport(handler))

        assert cap.method == "GET"
        assert cap.url == "https://example.com/"
        assert cap.source == "live"
        assert cap.response is not None
        assert cap.response.status_code == 200
        assert cap.response.body == b"hello"
        assert cap.response.headers["Content-Type"] == "text/plain"
        assert cap.timing.total_ms is not None
        assert cap.timing.total_ms >= 0

    def test_sets_default_user_agent(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("User-Agent", "")
            return httpx.Response(204)

        run("GET", "https://example.com/", transport=_transport(handler))
        assert seen["ua"] == DEFAULT_USER_AGENT

    def test_user_agent_can_be_overridden(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("User-Agent", "")
            return httpx.Response(204)

        run(
            "GET",
            "https://example.com/",
            headers={"User-Agent": "custom/1.0"},
            transport=_transport(handler),
        )
        assert seen["ua"] == "custom/1.0"

    def test_post_with_string_body(self):
        seen_body: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_body["body"] = request.content
            return httpx.Response(201)

        cap = run(
            "POST",
            "https://example.com/users",
            headers={"Content-Type": "application/json"},
            body='{"name":"alex"}',
            transport=_transport(handler),
        )

        assert seen_body["body"] == b'{"name":"alex"}'
        assert cap.body == b'{"name":"alex"}'
        assert cap.response is not None
        assert cap.response.status_code == 201

    def test_post_with_bytes_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "POST",
            "https://example.com/",
            body=b"\x00\x01\x02",
            transport=_transport(handler),
        )
        assert cap.body == b"\x00\x01\x02"

    def test_method_uppercased(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"  # httpx already uppercases
            return httpx.Response(200)

        cap = run("post", "https://example.com/", transport=_transport(handler))
        assert cap.method == "POST"

    def test_network_error_yields_no_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        cap = run("GET", "https://example.com/", transport=_transport(handler))
        assert cap.response is None
        assert cap.timing.total_ms is not None  # we still record how long we waited
        assert cap.source == "live"

    def test_default_reason_phrase_when_missing(self):
        # httpx leaves reason_phrase empty for HTTP/2; runner fills a sensible default.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        cap = run("GET", "https://example.com/", transport=_transport(handler))
        assert cap.response is not None
        assert cap.response.status_text  # not empty

    def test_4xx_status_is_captured_normally(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b'{"error":"unauthorized"}')

        cap = run("GET", "https://example.com/", transport=_transport(handler))
        assert cap.response is not None
        assert cap.response.status_code == 401
        assert cap.response.body == b'{"error":"unauthorized"}'

    @pytest.mark.integration
    def test_real_request_against_httpbin(self):
        cap = run("GET", "https://httpbin.org/status/200")
        assert cap.response is not None
        assert cap.response.status_code == 200
