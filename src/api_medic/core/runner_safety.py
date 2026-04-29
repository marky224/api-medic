"""SSRF guard for the hosted-demo Lambda's /api/run path.

The demo Lambda is publicly callable. Without a guard, anyone could ask
it to fetch RFC1918 addresses, the EC2 metadata service
(169.254.169.254), other AWS-internal endpoints, or loopback. This module
validates the URL and resolves DNS before any socket is opened, and
rejects anything that resolves to a special-use address.

The CLI and local web UI deliberately do NOT call this — they're not
public surfaces, and restricting them would block legitimate
localhost-debugging.

Note on TOCTOU: the resolved IPs are returned for diagnostic logging,
not used to pin httpx's connection. The window between resolve-and-check
and httpx's own DNS resolution is small (microseconds); the typical SSRF
attack here is "user submits a URL pointing at an internal target," not
DNS rebinding. Closing the TOCTOU gap fully would require a custom
httpcore transport — out of scope for v1, tracked in the architecture
doc as a hardening item.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


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

    # IP literal in URL: validate directly, no DNS needed.
    try:
        literal = ipaddress.ip_address(hostname)
        _check_ip_safe(literal)
        return [str(literal)]
    except ValueError:
        # Not an IP literal — proceed to DNS resolution.
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed for {hostname}: {e}") from e

    # info[4] is the sockaddr; for AF_INET that's (host, port), for AF_INET6
    # (host, port, flowinfo, scope). Host is always str — narrow for mypy.
    resolved = sorted({str(info[4][0]) for info in infos})
    if not resolved:
        raise UnsafeURLError(f"DNS returned no addresses for {hostname}.")

    for addr in resolved:
        _check_ip_safe(ipaddress.ip_address(addr))

    return resolved


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
