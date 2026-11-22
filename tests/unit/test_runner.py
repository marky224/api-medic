"""Tests for core.runner — live HTTP execution via httpx.

Most tests pass `probe_network=False` so they stay offline. Tests that
exercise the DNS/TLS pre-flight pieces monkeypatch `_resolve_dns` and
`_probe_tls` instead of hitting the network.
"""

from __future__ import annotations

import itertools
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import h11
import httpcore
import httpx
import pytest

from api_medic import __version__
from api_medic.core.captured import CapturedDns, CapturedTls
from api_medic.core.runner import DEFAULT_USER_AGENT, run, validate_request
from api_medic.core.runner_safety import InvalidRequestError, UnusableResponseError


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
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
        assert seen["ua"].startswith(f"api-medic/{__version__} ")

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

    def test_status_599_is_captured_normally(self) -> None:
        cap = run(
            "GET",
            "https://example.com/",
            transport=_transport(lambda request: httpx.Response(599)),
            probe_network=False,
        )
        assert cap.response is not None
        assert cap.response.status_code == 599

    @pytest.mark.parametrize("status", [600, 999])
    @pytest.mark.parametrize("redirected", [False, True], ids=["direct", "after-a-redirect"])
    def test_status_outside_100_599_is_unusable(self, status: int, redirected: bool) -> None:
        # h11 accepts any three-digit status; a Report can't hold these.
        def handler(request: httpx.Request) -> httpx.Response:
            if redirected and request.url.path == "/":
                return httpx.Response(302, headers={"Location": "/next"})
            return httpx.Response(status)

        with pytest.raises(UnusableResponseError) as e:
            run("GET", "https://example.com/", transport=_transport(handler), probe_network=False)
        assert str(e.value) == f"The server answered with status {status}, outside 100-599."


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
        def fake_resolve(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
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

        def fake_probe(host: str, port: int, timeout: float = 5.0) -> tuple[CapturedTls, float]:
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

        def fake_probe(host: str, port: int, timeout: float = 5.0) -> tuple[None, float]:
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


class TestUnsendableRedirectHop:
    def test_hop_that_fails_validate_url_is_not_connected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI and api-medic serve follow redirects through httpx, which
        doesn't range-check a Location's port; getaddrinfo would wrap 99999
        to 34463. The hop must not be connected, and the run reports no
        response. httpcore's TCP connect is stubbed: nothing is opened."""
        connects: list[tuple[str, int]] = []

        def connect_tcp(
            self: httpcore.SyncBackend, host: str, port: int, *a: Any, **kw: Any
        ) -> httpcore.NetworkStream:
            connects.append((host, port))
            if len(connects) == 1:
                return httpcore.MockStream(
                    [
                        b"HTTP/1.1 302 Found\r\nLocation: https://example.com:99999/\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    ]
                )
            return httpcore.MockStream([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"])

        # With no transport, httpx would send the first request to a proxy
        # named in the environment.
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
        cap = run("GET", "https://api.example.com/start", probe_network=False)

        assert cap.response is None
        assert connects == [("api.example.com", 443)]


_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_TOKEN_CHARACTERS = "letters, digits and !#$%&'*+-.^_`|~"
_AT_AN_END = "Header values can't start or end with a space or tab (header 'X-A')."


def _h11_sends(method: str, headers: dict[str, str]) -> bool:
    """Whether h11, which writes httpx's HTTP/1.1 requests, takes them."""
    try:
        h11.Request(method=method, target="/", headers=[("Host", "x"), *headers.items()])
    except h11.LocalProtocolError:
        return False
    return True


def _validate_request_sends(method: str, headers: dict[str, str]) -> bool:
    try:
        validate_request(method, "https://example.com/", headers)
    except InvalidRequestError:
        return False
    return True


class TestRequestH11CantSend:
    """h11 refuses a method or header name that isn't a token, and a header
    value holding NUL, LF, VT, FF or CR or starting or ending with a space
    or tab. httpx raised LocalProtocolError once the connection was open,
    so run() reported no response and gave no reason. It refuses them
    before anything is sent."""

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("X-A", "a\nb", "Header values can't hold '\\n' (header 'X-A')."),
            ("X-A", "a\rb", "Header values can't hold '\\r' (header 'X-A')."),
            ("X-A", "a\x0bb", "Header values can't hold '\\x0b' (header 'X-A')."),
            ("X-A", "a\x0cb", "Header values can't hold '\\x0c' (header 'X-A')."),
            ("X-A", "a\x00b", "Header values can't hold '\\x00' (header 'X-A')."),
            # Two curl -b values with a line break between them.
            ("Cookie", "a=1\n;b=2", "Header values can't hold '\\n' (header 'Cookie')."),
            ("X-A", " a", _AT_AN_END),
            ("X-A", "a ", _AT_AN_END),
            ("X-A", "\ta", _AT_AN_END),
            ("X-A", "a\t", _AT_AN_END),
            (
                "Bad Name",
                "v",
                f"Header names can only hold {_TOKEN_CHARACTERS} (header 'Bad Name').",
            ),
            ("", "v", f"Header names can only hold {_TOKEN_CHARACTERS} (header '')."),
            ("X:A", "v", f"Header names can only hold {_TOKEN_CHARACTERS} (header 'X:A')."),
            ("X\x1bA", "v", f"Header names can only hold {_TOKEN_CHARACTERS} (header 'X\\x1bA')."),
        ],
        ids=[
            "value-line-feed",
            "value-carriage-return",
            "value-vertical-tab",
            "value-form-feed",
            "value-nul",
            "value-line-break-between-cookies",
            "value-starting-with-a-space",
            "value-ending-with-a-space",
            "value-starting-with-a-tab",
            "value-ending-with-a-tab",
            "name-with-a-space",
            "name-empty",
            "name-with-a-colon",
            "name-with-esc",
        ],
    )
    def test_header_is_refused_before_anything_is_sent(
        self, name: str, value: str, message: str
    ) -> None:
        sent: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200)

        with pytest.raises(InvalidRequestError) as e:
            run(
                "GET",
                "https://example.com/",
                headers={name: value},
                transport=_transport(record),
                probe_network=False,
            )
        assert str(e.value) == message
        assert sent == []

    @pytest.mark.parametrize(
        ("method", "shown"),
        [("GE T", "'GE T'"), ("GET\x1b", "'GET\\x1b'"), ("", "''")],
        ids=["space", "esc", "empty"],
    )
    def test_method_is_refused_before_anything_is_sent(self, method: str, shown: str) -> None:
        sent: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200)

        with pytest.raises(InvalidRequestError) as e:
            run(method, "https://example.com/", transport=_transport(record), probe_network=False)
        assert str(e.value) == f"Method can only hold {_TOKEN_CHARACTERS} (got {shown})."
        assert sent == []

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("X-A", "a\x01b"),
            ("X-A", "a\x1bb"),
            ("X-A", "a\tb"),
            ("X-A", "a b"),
            ("X-A", ""),
            ("!#$%&'*+-.^_`|~09AZaz", "v"),
        ],
        ids=[
            "value-soh",
            "value-esc",
            "value-tab-inside",
            "value-space-inside",
            "value-empty",
            "name-every-token-character",
        ],
    )
    def test_header_h11_sends_is_still_sent(self, name: str, value: str) -> None:
        sent: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(request.headers[name])
            return httpx.Response(200)

        assert _h11_sends("GET", {name: value})
        cap = run(
            "GET",
            "https://example.com/",
            headers={name: value},
            transport=_transport(record),
            probe_network=False,
        )
        assert cap.response is not None
        assert sent == [value]

    def test_refuses_what_h11_refuses(self) -> None:
        """Each ASCII character in a header value, in a header name and in the
        method: validate_request refuses what h11 refuses, and only that."""
        cases: list[tuple[str, dict[str, str]]] = []
        for c in map(chr, range(128)):
            cases += [("GET", {"X-A": value}) for value in (f"a{c}b", f"{c}a", f"a{c}", c)]
            cases += [("GET", {name: "v"}) for name in (c, f"a{c}b")]
            cases += [(method, {}) for method in (c, f"G{c}T")]
        mismatches = [
            (method, headers, _h11_sends(method, headers))
            for method, headers in cases
            if _h11_sends(method, headers) != _validate_request_sends(method, headers)
        ]
        assert mismatches == []

    @pytest.mark.parametrize(
        "headers",
        [{"X-A": "a\nb"}, {"Bad Name": "v"}],
        ids=["value-line-break", "name-with-a-space"],
    )
    def test_header_h11_refuses_never_connects(
        self, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
    ) -> None:
        """Without a transport, httpx hands the request to h11 once the
        connection is open. httpcore's TCP connect is stubbed: nothing is
        opened."""
        connects: list[tuple[str, int]] = []

        def connect_tcp(
            self: httpcore.SyncBackend, host: str, port: int, *a: Any, **kw: Any
        ) -> httpcore.NetworkStream:
            connects.append((host, port))
            return httpcore.MockStream([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"])

        # With no transport, httpx would send the request to a proxy named
        # in the environment.
        for var in _PROXY_VARIABLES:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
        with pytest.raises(InvalidRequestError):
            run("GET", "https://api.example.com/", headers=headers, probe_network=False)
        assert connects == []

    def test_long_header_value_is_refused_quickly(self) -> None:
        started = time.perf_counter()
        with pytest.raises(InvalidRequestError):
            validate_request("GET", "https://example.com/", {"X-A": "a" * 200_000 + "\n"})
        elapsed = time.perf_counter() - started
        # Checked only when nothing is tracing, as in test_parser.py's
        # test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1


class _WireStream(httpcore.MockStream):
    """MockStream that records the bytes written to it. Its buffer is the
    test's list of responses, shared by every connection."""

    def __init__(self, buffer: list[bytes], written: list[bytes]) -> None:
        super().__init__(buffer)
        self._written = written

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._written.append(buffer)


_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def _httpx_defaults() -> bytes:
    """The headers every httpx client sends after Host. Accept-Encoding lists
    the decoders installed (brotli, zstandard), so it is read from httpx."""
    with httpx.Client() as client:
        accept_encoding = client.headers["Accept-Encoding"].encode()
    return b"Accept: */*\r\nAccept-Encoding: " + accept_encoding + b"\r\nConnection: keep-alive\r\n"


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """httpx's real transport, with httpcore's TCP connect stubbed: it records
    each connection and the bytes written, and answers from "responses".
    Nothing is opened. The proxy variables are cleared, or httpx would send
    the request to a proxy named in the environment."""
    state: dict[str, list[Any]] = {"connects": [], "written": [], "responses": [_OK]}

    def connect_tcp(
        self: httpcore.SyncBackend, host: str, port: int, *a: Any, **kw: Any
    ) -> httpcore.NetworkStream:
        state["connects"].append((host, port))
        return _WireStream(state["responses"], state["written"])

    for var in _PROXY_VARIABLES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    return state


_HELLO = "hello world"  # 11 bytes
_NOT_A_LENGTH = (
    "Content-Length must be one whole number of bytes, 1 to 20 digits (header 'Content-Length')."
)
_NOT_CHUNKED = "Transfer-Encoding can only be chunked (header 'Transfer-Encoding')."
_BOTH = "Content-Length and Transfer-Encoding can't be sent together; leave Content-Length out."


def _length_mismatch(length: int, size: int, name: str = "Content-Length") -> str:
    return (
        f"Content-Length is {length} but the body is {size} "
        f"byte{'' if size == 1 else 's'} (header {name!r})."
    )


def _h11_sends_framing(
    client: httpx.Client, method: str, headers: dict[str, str], body: bytes
) -> bool:
    """Whether h11 sends the request run() makes of these: httpx builds it,
    and a Transfer-Encoding given without a Content-Length goes without the
    one httpx adds; h11 then writes the request line and headers, the body
    and the end of the message."""
    request = client.build_request(
        method, "https://example.com/", headers=headers, content=body or None
    )
    names = {name.lower() for name in headers}
    if "transfer-encoding" in names and "content-length" not in names:
        request.headers.pop("Content-Length", None)
    connection = h11.Connection(our_role=h11.CLIENT)
    try:
        connection.send(
            h11.Request(
                method=request.method, target=request.url.raw_path, headers=request.headers.raw
            )
        )
        if request.content:
            connection.send(h11.Data(data=request.content))
        connection.send(h11.EndOfMessage())
    except h11.LocalProtocolError:
        return False
    return True


def _validate_request_sends_framing(method: str, headers: dict[str, str], body: bytes) -> bool:
    try:
        validate_request(method, "https://example.com/", headers, body or None)
    except InvalidRequestError:
        return False
    return True


# The lengths and codings the plan probed, and Python's int() limit.
_LENGTHS = [
    "11",
    "011",
    "00000000000000000011",
    "11, 11",
    "11,11",
    "0",
    "5",
    "20",
    "abc",
    "-1",
    "+11",
    "11.0",
    "1_1",
    "",
    "11,",
    ",11",
    "11, 5",
    "1" * 21,
    "1" * 4301,
    "0" * 4300 + "11",
]
_CODINGS = [
    "chunked",
    "CHUNKED",
    "Chunked",
    "gzip",
    "gzip, chunked",
    "chunked, gzip",
    "identity",
    "",
    "chunked, chunked",
    "chunked,",
]


class TestRequestFramingH11CantSend:
    """h11 writes a request's body as its Content-Length or Transfer-Encoding
    frames it. When the body didn't match the Content-Length, h11 raised its
    own LocalProtocolError while writing it, which escaped httpx: the CLI
    printed a traceback and both /api/run routes answered 500. A Content-
    Length that isn't one number, or a Transfer-Encoding other than chunked,
    h11 refused once the connection was open: a report with no response.
    They are refused before anything is looked up or connected."""

    @pytest.mark.parametrize(
        ("method", "length", "body", "message"),
        [
            ("POST", "5", _HELLO, _length_mismatch(5, 11)),
            ("POST", "20", _HELLO, _length_mismatch(20, 11)),
            ("POST", "0", _HELLO, _length_mismatch(0, 11)),
            ("GET", "5", None, _length_mismatch(5, 0)),
            # The body's length is its UTF-8 bytes: an e with an acute accent is two.
            ("POST", "1", "\u00e9", _length_mismatch(1, 2)),
        ],
        ids=["shorter", "longer", "zero-with-a-body", "length-without-a-body", "utf-8-bytes"],
    )
    def test_length_the_body_doesnt_match_is_refused_before_connecting(
        self,
        wire: dict[str, list[Any]],
        method: str,
        length: str,
        body: str | None,
        message: str,
    ) -> None:
        with pytest.raises(InvalidRequestError) as e:
            run(
                method,
                "https://api.example.com/",
                headers={"Content-Length": length},
                body=body,
                probe_network=False,
            )
        assert str(e.value) == message
        assert (wire["connects"], wire["written"]) == ([], [])

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            *(
                ("Content-Length", value, _NOT_A_LENGTH)
                for value in (
                    "abc",
                    "-1",
                    "+11",
                    "11.0",
                    "",
                    "11,",
                    "11, 5",
                    "1" * 21,
                    "1" * 4301,
                    "0" * 4300 + "11",
                )
            ),
            (
                "content-length",
                "abc",
                _NOT_A_LENGTH.replace("'Content-Length'", "'content-length'"),
            ),
            *(
                ("Transfer-Encoding", value, _NOT_CHUNKED)
                for value in ("gzip", "gzip, chunked", "identity", "", "chunked, chunked")
            ),
        ],
        ids=[
            "length-abc",
            "length-negative",
            "length-plus-sign",
            "length-decimal",
            "length-empty",
            "length-trailing-comma",
            "length-two-lengths",
            "length-21-digits",
            "length-4301-digits",
            "length-4302-digits-with-leading-zeros",
            "length-name-in-lowercase",
            "coding-gzip",
            "coding-gzip-then-chunked",
            "coding-identity",
            "coding-empty",
            "coding-chunked-twice",
        ],
    )
    def test_framing_h11_refuses_is_refused_before_connecting(
        self, wire: dict[str, list[Any]], name: str, value: str, message: str
    ) -> None:
        with pytest.raises(InvalidRequestError) as e:
            run(
                "POST",
                "https://api.example.com/",
                headers={name: value},
                body=_HELLO,
                probe_network=False,
            )
        assert str(e.value) == message
        assert (wire["connects"], wire["written"]) == ([], [])

    @pytest.mark.parametrize(
        ("length", "written"),
        [("11", b"11"), ("011", b"011"), ("11, 11", b"11")],
        ids=["eleven", "leading-zero", "same-length-twice"],
    )
    def test_length_h11_sends_is_still_sent(
        self, wire: dict[str, list[Any]], length: str, written: bytes
    ) -> None:
        cap = run(
            "POST",
            "https://api.example.com/",
            headers={"Content-Length": length},
            body=_HELLO,
            probe_network=False,
        )
        assert cap.response is not None
        sent = b"".join(wire["written"])
        assert b"\r\nContent-Length: " + written + b"\r\n" in sent
        assert sent.endswith(b"\r\n\r\nhello world")

    def test_coding_h11_sends_is_still_sent(self, wire: dict[str, list[Any]]) -> None:
        cap = run(
            "POST",
            "https://api.example.com/",
            headers={"Transfer-Encoding": "CHUNKED"},
            body=_HELLO,
            probe_network=False,
        )
        assert cap.response is not None
        sent = b"".join(wire["written"])
        assert b"\r\nTransfer-Encoding: chunked\r\n" in sent
        assert sent.endswith(b"\r\n\r\nb\r\nhello world\r\n0\r\n\r\n")

    def test_both_framing_headers_are_refused_before_connecting(
        self, wire: dict[str, list[Any]]
    ) -> None:
        """h11 sends both, and so does curl, but RFC 9112 section 6.2 forbids a
        sender to, and a server or proxy may read the body either way."""
        with pytest.raises(InvalidRequestError) as e:
            run(
                "POST",
                "https://api.example.com/",
                headers={"Content-Length": "11", "Transfer-Encoding": "chunked"},
                body=_HELLO,
                probe_network=False,
            )
        assert str(e.value) == _BOTH
        assert (wire["connects"], wire["written"]) == ([], [])

    @pytest.mark.parametrize(
        ("length", "coding", "message"),
        [
            ("abc", "gzip", _NOT_A_LENGTH),
            ("abc", "chunked", _NOT_A_LENGTH),
            ("11", "gzip", _NOT_CHUNKED),
            ("5", "gzip", _NOT_CHUNKED),
            ("11", "chunked", _BOTH),
            ("5", "chunked", _BOTH),
        ],
        ids=[
            "bad-length-and-bad-coding",
            "bad-length-and-chunked",
            "right-length-and-bad-coding",
            "wrong-length-and-bad-coding",
            "right-length-and-chunked",
            "wrong-length-and-chunked",
        ],
    )
    @pytest.mark.parametrize("coding_first", [False, True], ids=["length-first", "coding-first"])
    @pytest.mark.parametrize("as_pairs", [False, True], ids=["mapping", "pairs"])
    def test_request_breaking_several_framing_rules_gets_the_first_rules_message(
        self,
        wire: dict[str, list[Any]],
        length: str,
        coding: str,
        message: str,
        coding_first: bool,
        as_pairs: bool,
    ) -> None:
        """The framing rules are checked in one order, whatever the order of
        the headers: Content-Length's form, Transfer-Encoding's value, the
        two given together, the length against the body. A coding other than
        chunked given before a length that isn't a number got the coding's
        message, and the same two the other way round the length's."""
        pairs = [("Content-Length", length), ("Transfer-Encoding", coding)]
        if coding_first:
            pairs.reverse()
        with pytest.raises(InvalidRequestError) as e:
            run(
                "POST",
                "https://api.example.com/",
                headers=pairs if as_pairs else dict(pairs),
                body=_HELLO,
                probe_network=False,
            )
        assert str(e.value) == message
        assert (wire["connects"], wire["written"]) == ([], [])

    def test_refuses_what_h11_refuses(self) -> None:
        """Every ASCII character in, before and after a Content-Length and a
        Transfer-Encoding value, and the lengths and codings probed, on GET,
        POST, PUT and HEAD with bodies of 0, 5 and 11 bytes: validate_request
        refuses what an h11 dry run of run()'s request refuses, and only
        that. With both headers, it refuses at least what h11 refuses. One
        client builds every request: one each is a hundred times slower."""
        values = [
            *(
                ("Content-Length", v)
                for c in map(chr, range(128))
                for v in (f"1{c}1", f"{c}11", f"11{c}")
            ),
            *(
                ("Transfer-Encoding", v)
                for c in map(chr, range(128))
                for v in (f"chun{c}ked", f"{c}chunked", f"chunked{c}")
            ),
            *(("Content-Length", v) for v in _LENGTHS),
            *(("Transfer-Encoding", v) for v in _CODINGS),
        ]
        bodies = (b"", b"hello", b"hello world")
        methods = ("GET", "POST", "PUT", "HEAD")
        mismatches: list[tuple[str, dict[str, str], bytes, bool]] = []
        sent_with_both: list[tuple[str, dict[str, str], bytes]] = []
        with httpx.Client() as client:
            for (name, value), body, method in itertools.product(values, bodies, methods):
                headers = {name: value}
                h11_sends = _h11_sends_framing(client, method, headers, body)
                if h11_sends != _validate_request_sends_framing(method, headers, body):
                    mismatches.append((method, headers, body, h11_sends))
            for length, coding, body in itertools.product(_LENGTHS, _CODINGS, bodies):
                headers = {"Content-Length": length, "Transfer-Encoding": coding}
                if _validate_request_sends_framing("POST", headers, body):
                    sent_with_both.append(("POST", headers, body))
        assert mismatches == []
        assert sent_with_both == []

    @pytest.mark.parametrize(
        "length",
        ["1" * 200_000, ",".join(["1"] * 100_000), "1" + " " * 199_998 + "1"],
        ids=["digits", "commas", "spaces"],
    )
    def test_long_content_length_is_refused_quickly(self, length: str) -> None:
        # Digits, so a rule that read the number before counting its digits
        # would reach int(), which raises ValueError past 4,300 of them.
        started = time.perf_counter()
        with pytest.raises(InvalidRequestError):
            validate_request("POST", "https://example.com/", {"Content-Length": length}, _HELLO)
        elapsed = time.perf_counter() - started
        # Checked only when nothing is tracing, as in test_parser.py's
        # test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1


def _requests_written(written: list[bytes]) -> list[bytes]:
    """The request heads written, each up to its blank line."""
    sent = b"".join(written)
    return [b"POST " + part.split(b"\r\n\r\n", 1)[0] for part in sent.split(b"POST ")[1:]]


class TestRequestFramingAsCurlSendsIt:
    """A Transfer-Encoding the caller gave went with the Content-Length httpx
    adds for the body, which RFC 9112 forbids a sender to send, and a
    user-agent in another case went beside api-medic's User-Agent. curl
    sends neither. Through httpx's real transport, connect stubbed."""

    @pytest.mark.parametrize("coding", ["chunked", "CHUNKED"])
    def test_chunked_body_goes_without_a_content_length(
        self, wire: dict[str, list[Any]], coding: str
    ) -> None:
        cap = run(
            "POST",
            "https://api.example.com/",
            headers={"Transfer-Encoding": coding},
            body=_HELLO,
            probe_network=False,
        )
        assert cap.response is not None
        sent = b"".join(wire["written"])
        assert b"\r\nTransfer-Encoding: chunked\r\n" in sent
        assert b"Content-Length" not in sent
        assert sent.endswith(b"\r\n\r\nb\r\nhello world\r\n0\r\n\r\n")

    def test_redirect_hop_resends_it_the_same_way(self, wire: dict[str, list[Any]]) -> None:
        wire["responses"][:] = [
            b"HTTP/1.1 307 Temporary Redirect\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n",
            _OK,
        ]
        cap = run(
            "POST",
            "https://api.example.com/start",
            headers={"Transfer-Encoding": "chunked"},
            body=_HELLO,
            probe_network=False,
        )
        assert cap.response is not None
        assert cap.redirect_chain == [
            "https://api.example.com/start",
            "https://api.example.com/next",
        ]
        heads = _requests_written(wire["written"])
        assert [head.split(b"\r\n", 1)[0] for head in heads] == [
            b"POST /start HTTP/1.1",
            b"POST /next HTTP/1.1",
        ]
        assert all(b"\r\nTransfer-Encoding: chunked" in head for head in heads)
        assert all(b"Content-Length" not in head for head in heads)
        assert b"".join(wire["written"]).count(b"b\r\nhello world\r\n0\r\n\r\n") == 2

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_chunked_without_a_body_is_sent_as_before(
        self, wire: dict[str, list[Any]], method: str
    ) -> None:
        # httpx adds no Content-Length here, so there is none to drop.
        cap = run(
            method,
            "https://api.example.com/",
            headers={"Transfer-Encoding": "chunked"},
            probe_network=False,
        )
        assert cap.response is not None
        assert b"".join(wire["written"]) == (
            f"{method} / HTTP/1.1\r\nHost: api.example.com\r\n".encode()
            + _httpx_defaults()
            + b"Transfer-Encoding: chunked\r\nUser-Agent: "
            + DEFAULT_USER_AGENT.encode()
            + b"\r\n\r\n0\r\n\r\n"
        )

    @pytest.mark.parametrize("name", ["user-agent", "USER-AGENT", "User-Agent"])
    def test_user_agent_in_any_case_replaces_api_medics(
        self, wire: dict[str, list[Any]], name: str
    ) -> None:
        cap = run(
            "GET", "https://api.example.com/", headers={name: "mine/1.0"}, probe_network=False
        )
        sent = b"".join(wire["written"])
        assert sent.lower().count(b"\r\nuser-agent: ") == 1
        assert f"\r\n{name}: mine/1.0\r\n".encode() in sent
        assert cap.headers == {name: "mine/1.0"}

    def test_request_without_framing_headers_is_sent_as_before(
        self, wire: dict[str, list[Any]]
    ) -> None:
        cap = run(
            "POST",
            "https://api.example.com/users",
            headers={"Content-Type": "text/plain"},
            body=_HELLO,
            probe_network=False,
        )
        assert cap.response is not None
        assert b"".join(wire["written"]) == (
            b"POST /users HTTP/1.1\r\nHost: api.example.com\r\n"
            + _httpx_defaults()
            + b"Content-Type: text/plain\r\nUser-Agent: "
            + DEFAULT_USER_AGENT.encode()
            + b"\r\nContent-Length: 11\r\n\r\nhello world"
        )


def _given_twice(name: str) -> str:
    return (
        f"Header {name!r} is given more than once (names are case-insensitive, "
        "and a repeat with the same value counts too); give it once."
    )


class TestRequestHeaderGivenTwice:
    """A header name given twice, compared case-insensitively, is refused
    before anything is looked up or connected. Two names that differ only in
    case were sent as two headers, or h11 refused them once the connection
    was open (two Host or Transfer-Encoding headers, or two lengths)."""

    @pytest.mark.parametrize(
        ("headers", "repeat"),
        [
            ({"X-A": "1", "x-a": "2"}, "x-a"),
            ({"X-A": "1", "x-a": "1"}, "x-a"),
            ({"Host": "a.example", "host": "b.example"}, "host"),
            ({"Transfer-Encoding": "chunked", "transfer-encoding": "chunked"}, "transfer-encoding"),
            ({"Content-Length": "11", "content-length": "5"}, "content-length"),
            ({"Content-Length": "11", "content-length": "11"}, "content-length"),
            # The repeat is refused before the framing rules read either.
            ({"Content-Length": "11", "content-length": "abc"}, "content-length"),
        ],
        ids=[
            "two-cases",
            "two-cases-same-value",
            "host",
            "transfer-encoding",
            "two-lengths",
            "same-length",
            "before-the-framing-rules",
        ],
    )
    def test_name_in_two_cases_is_refused_before_connecting(
        self, wire: dict[str, list[Any]], headers: dict[str, str], repeat: str
    ) -> None:
        with pytest.raises(InvalidRequestError) as e:
            run(
                "POST",
                "https://api.example.com/",
                headers=headers,
                body=_HELLO,
                probe_network=False,
            )
        assert str(e.value) == _given_twice(repeat)
        assert (wire["connects"], wire["written"]) == ([], [])

    @pytest.mark.parametrize(
        "headers",
        [[("X-A", "1"), ("X-A", "2")], [("X-A", "1"), ("X-A", "1")], [("X-A", "1"), ("x-a", "2")]],
        ids=["one-spelling", "same-value", "two-cases"],
    )
    def test_list_with_a_name_twice_is_refused(
        self, wire: dict[str, list[Any]], headers: list[tuple[str, str]]
    ) -> None:
        # A list of pairs, as the CLI passes -H and a curl command's
        # headers; run() didn't take one, so this can't fail on the rule.
        with pytest.raises(InvalidRequestError) as e:
            run("GET", "https://api.example.com/", headers=headers, probe_network=False)
        assert str(e.value) == _given_twice(headers[1][0])
        assert (wire["connects"], wire["written"]) == ([], [])

    def test_list_without_a_repeat_is_sent_in_order(self, wire: dict[str, list[Any]]) -> None:
        cap = run(
            "GET",
            "https://api.example.com/",
            headers=[("X-B", "2"), ("X-A", "1")],
            probe_network=False,
        )
        assert cap.headers == {"X-B": "2", "X-A": "1", "User-Agent": DEFAULT_USER_AGENT}
        assert b"\r\nX-B: 2\r\nX-A: 1\r\n" in b"".join(wire["written"])

    def test_refuses_at_least_what_h11_refuses_for_a_name_in_two_cases(self) -> None:
        """A3's oracle leaves repeats out; here h11 refuses some (two Host or
        Transfer-Encoding headers, two lengths), and validate_request refuses
        all of them."""
        pairs = [
            *(("Content-Length", v) for v in ("11", "5", "abc")),
            *(("Transfer-Encoding", v) for v in ("chunked", "gzip")),
            *(("Host", v) for v in ("a.example", "")),
            ("X-A", "1"),
        ]
        sent: list[dict[str, str]] = []
        h11_refused = 0
        with httpx.Client() as client:
            for (name, first), (other, second) in itertools.product(pairs, pairs):
                if name != other:
                    continue
                headers = {name: first, name.lower(): second}
                h11_refused += not _h11_sends_framing(client, "POST", headers, b"hello world")
                if _validate_request_sends_framing("POST", headers, b"hello world"):
                    sent.append(headers)
        assert h11_refused > 0
        assert sent == []

    def test_many_headers_are_checked_quickly(self) -> None:
        headers = {f"X-{i}": "v" for i in range(100_000)}
        headers["x-99999"] = "v"
        started = time.perf_counter()
        with pytest.raises(InvalidRequestError) as e:
            validate_request("GET", "https://example.com/", headers)
        elapsed = time.perf_counter() - started
        assert str(e.value) == _given_twice("x-99999")
        # Checked only when nothing is tracing, as in test_parser.py's
        # test_command_at_the_argument_limit_parses_quickly.
        if sys.version_info < (3, 12) or sys.gettrace() is None:
            assert elapsed < 1
