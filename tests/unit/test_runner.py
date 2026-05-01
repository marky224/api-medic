"""Tests for core.runner — live HTTP execution via httpx.

Most tests pass `probe_network=False` so they stay offline. Tests that
exercise the DNS/TLS pre-flight pieces monkeypatch `_resolve_dns` and
`_probe_tls` instead of hitting the network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from api_medic.core.captured import CapturedDns, CapturedTls
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

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )

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

        run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
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
            probe_network=False,
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
            probe_network=False,
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
            probe_network=False,
        )
        assert cap.body == b"\x00\x01\x02"

    def test_method_uppercased(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"  # httpx already uppercases
            return httpx.Response(200)

        cap = run(
            "post",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.method == "POST"

    def test_network_error_yields_no_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.response is None
        assert cap.timing.total_ms is not None
        assert cap.source == "live"

    def test_default_reason_phrase_when_missing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.response is not None
        assert cap.response.status_text  # not empty

    def test_4xx_status_is_captured_normally(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b'{"error":"unauthorized"}')

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.response is not None
        assert cap.response.status_code == 401


class TestRedirectChain:
    def test_no_redirects_yields_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.redirect_chain is None

    def test_chain_contains_each_hop(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/start"):
                return httpx.Response(302, headers={"Location": "https://example.com/middle"})
            if url.endswith("/middle"):
                return httpx.Response(302, headers={"Location": "https://example.com/end"})
            return httpx.Response(200, content=b"end")

        cap = run(
            "GET",
            "https://example.com/start",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.redirect_chain == [
            "https://example.com/start",
            "https://example.com/middle",
            "https://example.com/end",
        ]


class TestRunnerEngineIntegration:
    """Exercise the live request path → engine end-to-end with mocked httpx,
    ensuring the same Report shape (and the same critical findings) the HAR
    path produces. These are regression tests for the architecture invariant
    that every surface produces byte-identical Reports for equivalent inputs.
    """

    def test_redirect_chain_of_ten_fires_too_many_redirects(self):
        from api_medic.core.engine import analyze

        def handler(request: httpx.Request) -> httpx.Response:
            # /step/N → /step/N-1, terminating at /step/0 with a 200.
            n = int(str(request.url).rsplit("/", 1)[-1])
            if n > 0:
                return httpx.Response(302, headers={"Location": f"/step/{n - 1}"})
            return httpx.Response(200, content=b"done")

        cap = run(
            "GET",
            "https://example.com/step/10",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.redirect_chain is not None
        assert len(cap.redirect_chain) == 11

        report = analyze(cap)
        ids = [f.id for f in report.findings]
        assert "http.redirect.too_many" in ids, (
            f"expected http.redirect.too_many in {ids}; live runner path "
            "should produce the same finding the engine fires for HAR fixtures."
        )
        finding = next(f for f in report.findings if f.id == "http.redirect.too_many")
        assert finding.severity == "critical"


class TestRawHeaders:
    def test_preserves_duplicate_set_cookie(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", "a=1"),
                    ("Set-Cookie", "b=2"),
                ],
            )

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.response is not None
        assert cap.response.raw_headers is not None
        cookies = [v for k, v in cap.response.raw_headers if k.lower() == "set-cookie"]
        assert cookies == ["a=1", "b=2"]


class TestContentEncodingStripping:
    """httpx auto-decompresses gzip/deflate/br on response.content. The
    captured headers should NOT then claim the body is still encoded —
    that would false-positive the encoding_mismatch check on every real
    site that ships compressed responses."""

    def test_strips_content_encoding_when_httpx_decoded_gzip(self):
        import gzip

        decoded = b"<!DOCTYPE html><html><body>hi</body></html>"
        gzipped = gzip.compress(decoded)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "text/html",
                    "Content-Encoding": "gzip",
                },
                content=gzipped,
            )

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )

        assert cap.response is not None
        # httpx returns the decoded body; the captured body matches.
        assert cap.response.body == decoded
        # And the captured headers no longer claim the body is gzip-encoded,
        # so checks against captured_response are internally consistent.
        assert "Content-Encoding" not in cap.response.headers
        assert all(k.lower() != "content-encoding" for k, _ in cap.response.raw_headers or [])

    def test_keeps_content_encoding_when_unknown_to_httpx(self):
        # An encoding httpx doesn't decompress (e.g. 'compress', 'identity')
        # leaves the body as-is; the header must be preserved so the body's
        # actual format is still described accurately.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "text/html",
                    "Content-Encoding": "compress",
                },
                content=b"some-compress-bytes",
            )

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )

        assert cap.response is not None
        assert cap.response.headers.get("Content-Encoding") == "compress"


class TestDnsProbing:
    def test_dns_populated_when_probing_enabled(self, monkeypatch):
        def fake_resolve(host: str, timeout: float = 5.0):
            return CapturedDns(records=["93.184.216.34"]), 11.5

        monkeypatch.setattr("api_medic.core.runner._resolve_dns", fake_resolve)
        monkeypatch.setattr(
            "api_medic.core.runner._probe_tls",
            lambda host, port, timeout=5.0: (None, 0.0),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=True,
        )
        assert cap.dns is not None
        assert cap.dns.records == ["93.184.216.34"]
        assert cap.timing.dns_ms == pytest.approx(11.5)

    def test_dns_skipped_when_probe_network_false(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=False,
        )
        assert cap.dns is None
        assert cap.timing.dns_ms is None


class TestTlsProbing:
    def test_https_request_probes_tls(self, monkeypatch):
        fake_tls = CapturedTls(
            not_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
            not_after=datetime(2027, 1, 1, tzinfo=timezone.utc),
            subject_common_name="example.com",
            subject_alt_names=["example.com", "www.example.com"],
            issuer_common_name="Let's Encrypt R3",
            negotiated_protocol_version="TLSv1.3",
        )

        def fake_probe(host: str, port: int, timeout: float = 5.0):
            assert host == "example.com"
            assert port == 443
            return fake_tls, 41.0

        monkeypatch.setattr("api_medic.core.runner._probe_tls", fake_probe)
        monkeypatch.setattr(
            "api_medic.core.runner._resolve_dns",
            lambda host, timeout=5.0: (CapturedDns(records=["1.2.3.4"]), 5.0),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=True,
        )
        assert cap.tls is not None
        assert cap.tls.subject_common_name == "example.com"
        assert "www.example.com" in cap.tls.subject_alt_names
        assert cap.tls.negotiated_protocol_version == "TLSv1.3"
        assert cap.timing.tls_ms == pytest.approx(41.0)

    def test_http_request_does_not_probe_tls(self, monkeypatch):
        called = {"probed": False}

        def fake_probe(host: str, port: int, timeout: float = 5.0):
            called["probed"] = True
            return None, 0.0

        monkeypatch.setattr("api_medic.core.runner._probe_tls", fake_probe)
        monkeypatch.setattr(
            "api_medic.core.runner._resolve_dns",
            lambda host, timeout=5.0: (CapturedDns(records=[]), 0.0),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        run(
            "GET",
            "http://example.com/",
            transport=_transport(handler),
            probe_network=True,
        )
        assert called["probed"] is False

    def test_failed_tls_probe_yields_none(self, monkeypatch):
        monkeypatch.setattr(
            "api_medic.core.runner._probe_tls",
            lambda host, port, timeout=5.0: (None, 12.0),
        )
        monkeypatch.setattr(
            "api_medic.core.runner._resolve_dns",
            lambda host, timeout=5.0: (CapturedDns(records=[]), 5.0),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(handler),
            probe_network=True,
        )
        assert cap.tls is None
        assert cap.timing.tls_ms == pytest.approx(12.0)


@pytest.mark.integration
def test_real_request_against_httpbin():
    cap = run("GET", "https://httpbin.org/status/200")
    assert cap.response is not None
    assert cap.response.status_code == 200
