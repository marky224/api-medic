"""SSRF guard for the hosted-demo Lambda's /api/run path.

The demo Lambda is publicly callable. Without a guard, anyone could ask
it to fetch RFC1918 addresses, the EC2 metadata service
(169.254.169.254), other AWS-internal endpoints, or loopback. This module
validates the URL and resolves DNS before any socket is opened, and
rejects anything that resolves to a special-use address.

The CLI and local web UI deliberately do NOT use the SSRF checks
(check_url_safe, the pinned connector) — they're not public surfaces,
and restricting them would block legitimate localhost-debugging. They
do use `validate_url` (through core.runner.run), which only rejects URLs
that can't be fetched at all, so those get a 400 instead of a 500.

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
import re
import socket
from collections.abc import Iterable
from urllib.parse import urlparse, urlsplit

import httpcore
import httpx


class UnsafeURLError(ValueError):
    """Raised when a URL fails the SSRF guard."""


class InvalidRequestError(ValueError):
    """Raised when a request can't be sent as given; callers answer 400."""


class InvalidURLError(InvalidRequestError):
    """Raised by validate_url for a URL that can't be fetched as given."""


# scheme "://" authority; the authority ends at the first "/", "?" or "#".
_AUTHORITY = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://([^/?#]*)")


def validate_url(url: str) -> None:
    """Reject a URL that can't be fetched as given, before any DNS lookup.

    Raises InvalidURLError with a message fit for a 400. Covers the URLs
    that used to surface as 500s: a port outside 1-65535 or not plain
    ASCII digits, square brackets around anything but an IPv6 address,
    a hostname httpx can't IDNA-decode or the socket module can't
    IDNA-encode, and anything else httpx or urllib refuses to parse.
    """
    _check_host_and_port(url)
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise InvalidURLError(f"Invalid URL: {e}") from e
    except UnicodeError as e:
        raise InvalidURLError(
            "Invalid URL: it contains characters that can't be encoded as UTF-8."
        ) from e
    try:
        raw_host = parsed.raw_host.decode("ascii")
    except UnicodeError as e:
        # httpx accepts a non-ASCII IPv6 zone ID ("[fe80::1%25é]") on parse
        # but can't encode it for the request.
        raise InvalidURLError(f"Invalid URL: the host contains non-ASCII characters ({e}).") from e
    try:
        parsed.host  # noqa: B018 — decodes xn-- labels, which httpx doesn't check on parse
    except UnicodeError as e:
        raise InvalidURLError(
            f"Invalid URL: {raw_host!r} is not a valid internationalized hostname ({e})."
        ) from e
    try:
        split = urlsplit(url)
        split.port  # noqa: B018 — urllib's own checks, which the TLS probe relies on
        typed_host = split.hostname
    except ValueError as e:
        raise InvalidURLError(f"Invalid URL: {e}") from e
    # getaddrinfo IDNA-encodes (IDNA 2003) the name before looking it up:
    # the TLS probe passes it the name as typed, httpx its own encoding.
    for name in (typed_host, raw_host):
        if not name:
            continue
        try:
            name.encode("idna")
        except UnicodeError as e:
            raise InvalidURLError(
                f"Invalid URL: hostname {name!r} can't be looked up in DNS ({e})."
            ) from e


def _check_host_and_port(url: str) -> None:
    """Check brackets and port in the URL's authority as written.

    Done by hand because httpx and urllib disagree here, and urllib's
    answer depends on the Python version: httpx takes ':+443' as 443 and
    ':٣' as port 3, older urllib takes '[example.com]' as a hostname.
    """
    match = _AUTHORITY.match(url.strip())
    if match is None:
        return
    host_port = match.group(1).rpartition("@")[2]
    if host_port.startswith("["):
        host, closed, rest = host_port[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")) or not _is_ipv6(host):
            raise InvalidURLError(_brackets_message(host_port))
        port = rest[1:]
    elif "[" in host_port or "]" in host_port:
        raise InvalidURLError(_brackets_message(host_port))
    else:
        port = host_port.partition(":")[2]
    if port and not _is_valid_port(port):
        raise InvalidURLError(
            f"Invalid URL: port must be a whole number from 1 to 65535 (got {port!r})."
        )


def _is_valid_port(port: str) -> bool:
    if not (port.isascii() and port.isdigit()):
        return False
    # Cap the length before int(), which refuses strings over 4,300 digits.
    # Leading zeros don't count, so ':0443' stays valid.
    digits = port.lstrip("0")
    return len(digits) <= 5 and 1 <= int(digits or "0") <= 65535


def _brackets_message(host_port: str) -> str:
    return (
        f"Invalid URL: square brackets are only allowed around an IPv6 address (got {host_port!r})."
    )


def _is_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def check_url_safe(url: str) -> list[str]:
    """Validate `url` is safe for the public Lambda to fetch.

    Returns the resolved IPs (for diagnostic logging). Raises
    InvalidURLError for a URL validate_url rejects, UnsafeURLError on any
    safety check failure.

    Rules:
    - The URL must pass validate_url.
    - Scheme must be https.
    - Hostname must parse.
    - Hostname-as-IP-literal must not be in any blocked range.
    - DNS resolution must succeed and yield only public addresses.
    """
    validate_url(url)
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


# IPv6 ranges whose last 32 bits are an IPv4 address: IPv4-compatible
# (::a.b.c.d, deprecated) and the NAT64 well-known prefix. Where they're
# routed, a connection to one reaches that IPv4 address.
_EMBEDS_IPV4 = (ipaddress.IPv6Network("::/96"), ipaddress.IPv6Network("64:ff9b::/96"))


def _check_ip_safe(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    embedded_in: ipaddress.IPv6Address | None = None,
) -> None:
    name = str(ip) if embedded_in is None else f"{ip} (embedded in {embedded_in})"
    # Order matters: link-local and loopback are technically subsets of
    # is_private in Python's classification. Check the more specific
    # categories first so the error message names the actual reason
    # (e.g. "link-local 169.254.169.254" rather than "private").
    if ip.is_loopback:
        raise UnsafeURLError(f"Refusing to fetch loopback address {name}.")
    if ip.is_link_local:
        raise UnsafeURLError(f"Refusing to fetch link-local address {name}.")
    if ip.is_multicast:
        raise UnsafeURLError(f"Refusing to fetch multicast address {name}.")
    if ip.is_unspecified:
        raise UnsafeURLError(f"Refusing to fetch unspecified address {name}.")
    if isinstance(ip, ipaddress.IPv6Address):
        # Deprecated site-local fec0::/10 still passes is_global.
        if ip.is_site_local:
            raise UnsafeURLError(f"Refusing to fetch site-local address {name}.")
        if any(ip in net for net in _EMBEDS_IPV4):
            _check_ip_safe(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF), embedded_in=ip)
    if ip.is_private:
        raise UnsafeURLError(f"Refusing to fetch private address {name}.")
    if ip.is_reserved:
        raise UnsafeURLError(f"Refusing to fetch reserved address {name}.")
    # Catch-all for ranges the named checks above miss, e.g. CGNAT
    # 100.64.0.0/10 is neither private nor global.
    if not ip.is_global:
        raise UnsafeURLError(f"Refusing to fetch non-public address {name}.")
