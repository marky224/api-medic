from __future__ import annotations

import pytest

from api_medic.core.runner_safety import UnsafeURLError, check_url_safe

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
    ],
)
def test_rejects_unsafe_ip_literal(ip_literal: str):
    with pytest.raises(UnsafeURLError):
        check_url_safe(f"https://{ip_literal}/path")


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
