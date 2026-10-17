"""SSRF guard for the hosted-demo Lambda's /api/run path.

The demo Lambda is publicly callable. Without a guard, anyone could ask
it to fetch RFC1918 addresses, the EC2 metadata service
(169.254.169.254), other AWS-internal endpoints, or loopback. This module
validates the URL and resolves DNS before any socket is opened, and
rejects anything that resolves to a special-use address.

The CLI and local web UI deliberately do NOT call this — they're not
public surfaces, and restricting them would block legitimate
localhost-debugging.

Connections are pinned. Checking a name and then letting httpx (or the
runner's TLS probe) resolve it again would leave a window of seconds in
which a low-TTL DNS-rebinding answer could swap in an internal address.
So every connection on the guarded path goes through `connect_pinned`
or `PinnedTransport`: each resolves the name once, rejects the lot if
any address isn't public, and connects to the checked IP itself. Host,
SNI and certificate checks still use the original name.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

import httpcore
import httpx


class UnsafeURLError(ValueError):
    """Raised when a URL fails the SSRF guard."""


def check_url_safe(url: str) -> list[str]:
    """Validate `url` is safe for the public Lambda to fetch.

    Returns the resolved IPs (for diagnostic logging). Raises
    UnsafeURLError on any safety check failure.

    Rules:
    - Scheme must be https.
    - Hostname must parse.
    - Hostname-as-IP-literal must not be in any blocked range.
    - DNS resolution must succeed and yield only public addresses.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise UnsafeURLError(
            f"Only https:// URLs are accepted (got {parsed.scheme or 'no scheme'})."
        )

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL has no hostname.")

    # getaddrinfo IDNA-encodes the name first and raises UnicodeError, not
    # gaierror, for labels it can't encode (empty, or over 63 characters).
    try:
        return sorted(resolve_public(hostname))
    except (socket.gaierror, UnicodeError) as e:
        raise UnsafeURLError(f"DNS resolution failed for {hostname}: {e}") from e


def resolve_public(host: str) -> list[str]:
    """Resolve `host` once and return its addresses in resolver order.

    Raises UnsafeURLError if any address isn't public (one bad address
    rejects the lot) or there are none. An IP literal is checked without
    a lookup. Resolution failures propagate as socket.gaierror (an
    OSError) or UnicodeError.
    """
    # The check stays outside the try: UnsafeURLError is a ValueError and
    # must not be swallowed as "not an IP literal".
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _check_ip_safe(literal)
        return [str(literal)]

    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    # info[4] is the sockaddr; for AF_INET that's (host, port), for AF_INET6
    # (host, port, flowinfo, scope). Host is always str — narrow for mypy.
    addrs = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addrs:
        raise UnsafeURLError(f"DNS returned no addresses for {host}.")
    for addr in addrs:
        _check_ip_safe(ipaddress.ip_address(addr))
    return addrs


def connect_pinned(host: str, port: int, timeout: float | None) -> socket.socket:
    """Open a TCP connection to `host` using only addresses checked just now.

    Tries each address in resolver order, as socket.create_connection
    does; the last one's error propagates.
    """
    addrs = resolve_public(host)
    for addr in addrs[:-1]:
        try:
            return socket.create_connection((addr, port), timeout)
        except OSError:
            continue
    return socket.create_connection((addrs[-1], port), timeout)


class _PinnedBackend(httpcore.SyncBackend):
    """httpcore backend that connects only to addresses checked just now.

    httpcore passes the URL's host here and does TLS afterwards with the
    same host as SNI, so connecting to the checked IP leaves Host, SNI and
    certificate verification on the original name.
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            addrs = resolve_public(host)
        except OSError as e:
            # A failed lookup is a network failure, as it is for httpx.
            raise httpcore.ConnectError(str(e)) from e
        for addr in addrs[:-1]:
            try:
                return super().connect_tcp(addr, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        return super().connect_tcp(addrs[-1], port, timeout, local_address, socket_options)


class PinnedTransport(httpx.HTTPTransport):
    """httpx transport whose every connection goes through `_PinnedBackend`.

    It never uses a proxy: a proxy would resolve the name itself, out of
    reach of the check. Passing a transport to httpx.Client also turns off
    its HTTP(S)_PROXY environment handling.

    httpx.HTTPTransport keeps its connection pool in `_pool` and has no
    option for a network backend, so this swaps the pool for one that
    uses ours. tests/unit/test_runner_safety.py fails if that attribute
    goes away.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=_PinnedBackend(),
        )


def _check_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    # Order matters: link-local and loopback are technically subsets of
    # is_private in Python's classification. Check the more specific
    # categories first so the error message names the actual reason
    # (e.g. "link-local 169.254.169.254" rather than "private").
    if ip.is_loopback:
        raise UnsafeURLError(f"Refusing to fetch loopback address {ip}.")
    if ip.is_link_local:
        raise UnsafeURLError(f"Refusing to fetch link-local address {ip}.")
    if ip.is_multicast:
        raise UnsafeURLError(f"Refusing to fetch multicast address {ip}.")
    if ip.is_unspecified:
        raise UnsafeURLError(f"Refusing to fetch unspecified address {ip}.")
    if ip.is_private:
        raise UnsafeURLError(f"Refusing to fetch private address {ip}.")
    if ip.is_reserved:
        raise UnsafeURLError(f"Refusing to fetch reserved address {ip}.")
    # Catch-all for ranges the named checks above miss, e.g. CGNAT
    # 100.64.0.0/10 is neither private nor global.
    if not ip.is_global:
        raise UnsafeURLError(f"Refusing to fetch non-public address {ip}.")
