from __future__ import annotations

import re
import socket
import ssl
from typing import Any

import httpcore
import httpx
import pytest

from api_medic.core.runner_safety import (
    InvalidURLError,
    PinnedTransport,
    UnsafeURLError,
    check_url_safe,
    connect_pinned,
    validate_url,
)

# ---- Scheme / parse ---------------------------------------------------


def test_rejects_http_scheme():
    with pytest.raises(UnsafeURLError, match="Only https"):
        check_url_safe("http://example.com/")


def test_rejects_other_schemes():
    for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://example.com/"):
        with pytest.raises(UnsafeURLError, match="Only https"):
            check_url_safe(url)


def test_rejects_no_hostname():
    with pytest.raises(UnsafeURLError, match="no hostname"):
        check_url_safe("https:///path")


# ---- IP literals ------------------------------------------------------


@pytest.mark.parametrize(
    "ip_literal",
    [
        "127.0.0.1",  # loopback
        "127.0.0.5",  # loopback range
        "10.0.0.1",  # RFC1918
        "192.168.1.1",  # RFC1918
        "172.16.0.1",  # RFC1918
        "169.254.169.254",  # EC2 metadata service (link-local)
        "169.254.0.1",  # link-local
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local (private)
        "ff02::1",  # IPv6 multicast
        "100.64.0.1",  # CGNAT shared address space (not private, not global)
    ],
)
def test_rejects_unsafe_ip_literal(ip_literal: str) -> None:
    # IPv6 literals need brackets in a URL; without them the host is
    # malformed and the IP rule is never reached.
    host = f"[{ip_literal}]" if ":" in ip_literal else ip_literal
    with pytest.raises(UnsafeURLError):
        check_url_safe(f"https://{host}/path")


def test_rejects_cgnat_as_non_public():
    # 100.64.0.0/10 is neither is_private nor is_global, so only the
    # catch-all `not is_global` rule names it.
    with pytest.raises(UnsafeURLError, match=r"non-public address 100\.64\.0\.1"):
        check_url_safe("https://100.64.0.1/")


def test_unsafe_ip_literal_rejected_without_dns(monkeypatch):
    """A blocked literal must be rejected by the literal check itself, not
    rescued by falling through to DNS."""
    import socket

    def no_dns(*a, **kw):
        raise AssertionError("IP literal should not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(UnsafeURLError, match="loopback"):
        check_url_safe("https://127.0.0.1/")


def test_accepts_public_ip_literal():
    # 8.8.8.8 (Google DNS) is public.
    result = check_url_safe("https://8.8.8.8/")
    assert result == ["8.8.8.8"]


# ---- Hostname resolution ----------------------------------------------


def test_accepts_hostname_resolving_to_public_ip(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    result = check_url_safe("https://example.com/")
    assert result == ["93.184.216.34"]


def test_rejects_hostname_resolving_to_private_ip(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(UnsafeURLError, match="private"):
        check_url_safe("https://internal.example.com/")


def test_rejects_hostname_resolving_to_metadata_service(monkeypatch):
    """DNS rebinding flavour: hostname looks public but resolves to AWS metadata."""
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
    )
    with pytest.raises(UnsafeURLError, match="link-local"):
        check_url_safe("https://my-attack-domain.example/")


def test_rejects_when_any_resolved_address_is_private(monkeypatch):
    """If a hostname returns multiple IPs and any are private, reject the lot."""
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0)),
        ],
    )
    with pytest.raises(UnsafeURLError, match="private"):
        check_url_safe("https://multi.example/")


def test_dns_failure_surfaces_as_unsafe_url_error(monkeypatch):
    import socket

    def fail(*a, **kw):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
        check_url_safe("https://does-not-exist.example/")


def test_unencodable_hostname_surfaces_as_unsafe_url_error(monkeypatch):
    """getaddrinfo IDNA-encodes the name first and raises UnicodeError (not
    gaierror) for labels it can't encode. validate_url now rejects those
    names before any lookup (see test_validate_url_*), but a UnicodeError
    from getaddrinfo must still surface as UnsafeURLError."""
    import socket

    def fail(*a, **kw):
        raise UnicodeError("label empty or too long")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
        check_url_safe("https://example.com/")


# ---- Connection pinning -----------------------------------------------


def _scripted_resolver(monkeypatch: pytest.MonkeyPatch, *answers: list[str]) -> list[str]:
    """Fake getaddrinfo: lookup N gets answers[N] (the last one repeats), the
    way a DNS-rebinding server can answer the guard and the connection
    differently. Returns the names looked up, in order."""
    lookups: list[str] = []
    queue = list(answers)

    def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
        lookups.append(host)
        ips = queue.pop(0) if len(queue) > 1 else queue[0]
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return lookups


def _record_connects(
    monkeypatch: pytest.MonkeyPatch, refuse: frozenset[str] = frozenset()
) -> tuple[list[tuple[str, int]], object]:
    """Record socket.create_connection calls instead of connecting. Returns
    the record and the object a successful call returns."""
    connects: list[tuple[str, int]] = []
    sock = object()

    def create_connection(address: tuple[str, int], *a: Any, **kw: Any) -> object:
        connects.append(address)
        if address[0] in refuse:
            raise ConnectionRefusedError("refused")
        return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)
    return connects, sock


def test_connect_pinned_refuses_a_private_second_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = _scripted_resolver(monkeypatch, ["93.184.216.34"], ["10.0.0.5"])
    connects, _ = _record_connects(monkeypatch)

    assert check_url_safe("https://rebind.example/") == ["93.184.216.34"]
    with pytest.raises(UnsafeURLError, match=r"private address 10\.0\.0\.5"):
        connect_pinned("rebind.example", 443, 5.0)
    assert lookups == ["rebind.example", "rebind.example"]
    assert connects == []


def test_connect_pinned_connects_to_the_address_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = _scripted_resolver(monkeypatch, ["93.184.216.34"], ["93.184.216.35"])
    connects, sock = _record_connects(monkeypatch)

    check_url_safe("https://api.example.com/")
    assert connect_pinned("api.example.com", 443, 5.0) is sock
    # The IP literal goes to the socket layer, so nothing resolves the name again.
    assert connects == [("93.184.216.35", 443)]
    assert len(lookups) == 2


def test_connect_pinned_tries_addresses_in_resolver_order(monkeypatch: pytest.MonkeyPatch) -> None:
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    _scripted_resolver(monkeypatch, ["93.184.216.35", v6, "93.184.216.34"])
    connects, sock = _record_connects(monkeypatch, refuse=frozenset({"93.184.216.35"}))

    assert connect_pinned("api.example.com", 8443, 5.0) is sock
    assert connects == [("93.184.216.35", 8443), (v6, 8443)]


def test_connect_pinned_rejects_all_if_any_address_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted_resolver(monkeypatch, ["93.184.216.34", "192.168.1.1"])
    connects, _ = _record_connects(monkeypatch)

    with pytest.raises(UnsafeURLError, match="private"):
        connect_pinned("multi.example", 443, 5.0)
    assert connects == []


class _RecordingStream(httpcore.MockStream):
    """MockStream that records the TLS handshake parameters and bytes sent."""

    def __init__(self, buffer: list[bytes], log: dict[str, list[Any]]) -> None:
        super().__init__(buffer)
        self._log = log

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self._log["tls"].append(
            (server_hostname, ssl_context.check_hostname, ssl_context.verify_mode)
        )
        return self

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._log["sent"].append(buffer)


def _record_tcp(monkeypatch: pytest.MonkeyPatch, *responses: bytes) -> dict[str, list[Any]]:
    """Replace httpcore's real TCP connect: record (host, port), answer from
    `responses` (one per connection) over a _RecordingStream."""
    log: dict[str, list[Any]] = {"tcp": [], "tls": [], "sent": []}
    queue = list(responses)

    def connect_tcp(
        self: httpcore.SyncBackend, host: str, port: int, *a: Any, **kw: Any
    ) -> httpcore.NetworkStream:
        log["tcp"].append((host, port))
        return _RecordingStream([queue.pop(0)], log)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    return log


_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def test_pinned_transport_connects_to_the_checked_ip_and_keeps_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted_resolver(monkeypatch, ["93.184.216.34"])
    log = _record_tcp(monkeypatch, _OK)

    with httpx.Client(transport=PinnedTransport()) as client:
        response = client.get("https://api.example.com/v1")

    assert response.status_code == 200
    assert log["tcp"] == [("93.184.216.34", 443)]
    # SNI and certificate verification use the name, not the IP.
    assert log["tls"] == [("api.example.com", True, ssl.CERT_REQUIRED)]
    assert b"Host: api.example.com\r\n" in b"".join(log["sent"])


def test_pinned_transport_refuses_a_private_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_resolver(monkeypatch, ["169.254.169.254"])
    log = _record_tcp(monkeypatch, _OK)

    with (
        httpx.Client(transport=PinnedTransport()) as client,
        pytest.raises(UnsafeURLError, match="link-local"),
    ):
        client.get("https://api.example.com/")
    assert log["tcp"] == []


def test_pinned_transport_turns_a_failed_lookup_into_a_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*a: Any, **kw: Any) -> list[Any]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with httpx.Client(transport=PinnedTransport()) as client, pytest.raises(httpx.ConnectError):
        client.get("https://does-not-exist.example/")


def test_httpx_transport_still_keeps_its_pool_in_the_pool_attribute() -> None:
    """PinnedTransport replaces httpx.HTTPTransport._pool with a pinned pool.
    If httpx stops keeping its pool there, the swap would silently stop
    pinning; test_pinned_transport_connects_to_the_checked_ip_... would then
    see the hostname reach the socket layer."""
    assert isinstance(getattr(httpx.HTTPTransport(), "_pool", None), httpcore.ConnectionPool), (
        "httpx.HTTPTransport no longer has a _pool ConnectionPool; "
        "update PinnedTransport in core/runner_safety.py"
    )
    assert isinstance(PinnedTransport()._pool, httpcore.ConnectionPool)


# ---- validate_url -------------------------------------------------------

_PORT = "port must be a whole number from 1 to 65535"
_BRACKETS = "square brackets are only allowed around an IPv6 address"
_IDNA = "is not a valid internationalized hostname"
_DNS = "can't be looked up in DNS"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://example.com:99999/", _PORT),
        ("https://example.com:65536/", _PORT),
        ("https://example.com:0/", _PORT),
        ("https://example.com:-1/", _PORT),
        ("https://example.com:+443/", _PORT),  # httpx reads this as 443
        ("https://example.com:٣/", _PORT),  # Arabic-Indic 3; httpx reads it as port 3
        ("https://[2001:db8::1]:99999/", _PORT),
        ("https://[example.com]/", _BRACKETS),
        ("https://[1.2.3.4]/", _BRACKETS),
        ("https://[::1/", _BRACKETS),
        ("https://ex[ample].com/", _BRACKETS),
        ("https://xn--zz.example/", _IDNA),
        ("https://xn--ls8h.la/", _IDNA),
        ("https://" + "a" * 64 + ".example/", _DNS),
        ("https://a..b.example/", _DNS),
        ("https://exa\tmple.com/", "Invalid URL"),
        ("https://example.com/\ud800", "can't be encoded as UTF-8"),
    ],
)
def test_validate_url_rejects(url: str, message: str) -> None:
    with pytest.raises(InvalidURLError, match=re.escape(message)):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://example.com:443/",
        "https://example.com:/",
        "https://user:pw@example.com:8443/path?q=1#frag",
        "https://[2001:db8::1]:8443/",
        "http://localhost:8765/api",
        "https://xn--x-qfa.example/",
        "https://xß.example/",
    ],
)
def test_validate_url_accepts(url: str) -> None:
    validate_url(url)


def test_check_url_safe_validates_before_any_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = _scripted_resolver(monkeypatch, ["93.184.216.34"])
    with pytest.raises(InvalidURLError, match=_BRACKETS):
        check_url_safe("https://[example.com]/")
    assert lookups == []
