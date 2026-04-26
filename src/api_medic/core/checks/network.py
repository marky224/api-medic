"""Network & transport diagnostic checks.

All five checks ride on data populated by the live Runner — they no-op for
HAR/curl sources (where DNS/TLS metadata isn't available).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..captured import CapturedRequest
from ..models import Finding
from . import register

DNS_SLOW_MS = 500.0
TLS_EXPIRING_SOON_DAYS = 14
WEAK_TLS_PROTOCOLS = ("TLSv1", "TLSv1.0", "TLSv1.1", "SSLv2", "SSLv3")


@register
def dns_no_records(captured: CapturedRequest) -> Finding | None:
    if captured.dns is None:
        return None
    if captured.dns.records:
        return None

    host = urlparse(captured.url).hostname or ""
    return Finding(
        id="network.dns.no_records",
        severity="critical",
        title="DNS lookup returned no records",
        explanation=(
            f"No A or AAAA records were found for {host}. The request can't "
            "reach the server because the host doesn't resolve to an IP."
        ),
        evidence={"host": host},
        suggested_fix=(
            "Check the URL for typos. If the domain is correct, the DNS "
            "record may be missing or your resolver may be misconfigured."
        ),
    )


@register
def dns_slow(captured: CapturedRequest) -> Finding | None:
    dns_ms = captured.timing.dns_ms
    if dns_ms is None or dns_ms <= DNS_SLOW_MS:
        return None
    if captured.dns is None or not captured.dns.records:
        # If DNS failed entirely, dns_no_records is the right finding to fire.
        return None

    return Finding(
        id="network.dns.slow",
        severity="warning",
        title="DNS resolution was slow",
        explanation=(
            f"DNS resolution took {dns_ms:.0f} ms (threshold: {DNS_SLOW_MS:.0f} ms). "
            "This adds latency to every request. Often a sign of a misconfigured "
            "or geographically distant resolver."
        ),
        evidence={"dns_ms": round(dns_ms, 1), "threshold_ms": DNS_SLOW_MS},
        suggested_fix=(
            "Switch to a faster recursive resolver (1.1.1.1, 8.8.8.8) or "
            "investigate the host's authoritative DNS configuration."
        ),
    )


@register
def tls_expired(captured: CapturedRequest) -> Finding | None:
    if captured.tls is None:
        return None
    now = datetime.now(timezone.utc)
    if captured.tls.not_after >= now:
        return None
    expired_for = now - captured.tls.not_after
    return Finding(
        id="network.tls.expired",
        severity="critical",
        title="TLS certificate has expired",
        explanation=(
            f"The server's certificate expired {_humanize_delta(expired_for)} ago. "
            "Modern clients refuse to negotiate against expired certs."
        ),
        evidence={
            "not_after": _iso(captured.tls.not_after),
            "expired_for_seconds": int(expired_for.total_seconds()),
        },
        suggested_fix=(
            "Renew the certificate on the server. If you control the host, "
            "automate via certbot / ACME so this doesn't recur."
        ),
    )


@register
def tls_expiring_soon(captured: CapturedRequest) -> Finding | None:
    if captured.tls is None:
        return None
    now = datetime.now(timezone.utc)
    if captured.tls.not_after < now:
        return None  # already expired — tls_expired handles it
    threshold = now + timedelta(days=TLS_EXPIRING_SOON_DAYS)
    if captured.tls.not_after > threshold:
        return None
    expires_in = captured.tls.not_after - now
    return Finding(
        id="network.tls.expiring_soon",
        severity="warning",
        title=f"TLS certificate expires in under {TLS_EXPIRING_SOON_DAYS} days",
        explanation=(
            f"The server's certificate expires in {_humanize_delta(expires_in)}. "
            "Renew before then or clients will start failing handshakes."
        ),
        evidence={
            "not_after": _iso(captured.tls.not_after),
            "expires_in_seconds": int(expires_in.total_seconds()),
            "threshold_days": TLS_EXPIRING_SOON_DAYS,
        },
        suggested_fix="Schedule a renewal — ideally automated via certbot / ACME.",
    )


@register
def tls_weak_protocol(captured: CapturedRequest) -> Finding | None:
    if captured.tls is None:
        return None
    version = captured.tls.negotiated_protocol_version
    if not version or version not in WEAK_TLS_PROTOCOLS:
        return None
    return Finding(
        id="network.tls.weak_protocol",
        severity="critical",
        title=f"Weak TLS protocol negotiated ({version})",
        explanation=(
            f"The connection negotiated {version}. Anything below TLS 1.2 is "
            "considered insecure and is rejected by modern clients (browsers, "
            "Python 3.12+, recent OpenSSL builds)."
        ),
        evidence={"negotiated_version": version, "minimum_safe_version": "TLSv1.2"},
        suggested_fix=(
            "Disable TLS 1.0 and 1.1 on the server. With nginx: `ssl_protocols TLSv1.2 TLSv1.3;`."
        ),
    )


@register
def tls_cn_mismatch(captured: CapturedRequest) -> Finding | None:
    if captured.tls is None:
        return None
    host = (urlparse(captured.url).hostname or "").lower()
    if not host:
        return None

    cn = (captured.tls.subject_common_name or "").lower()
    sans = [s.lower() for s in captured.tls.subject_alt_names]

    if _name_matches(host, cn) or any(_name_matches(host, s) for s in sans):
        return None

    return Finding(
        id="network.tls.cn_mismatch",
        severity="critical",
        title="TLS certificate doesn't match the requested host",
        explanation=(
            f"The certificate is issued for {cn or '(no CN)'} (SANs: "
            f"{', '.join(sans) or 'none'}), but the request was for {host}. "
            "Browsers will refuse to connect; non-browser clients should too."
        ),
        evidence={
            "request_host": host,
            "cert_common_name": cn or None,
            "cert_subject_alt_names": sans,
        },
        suggested_fix=(
            "Reissue the certificate with the correct SAN list, or fix the URL the client is using."
        ),
    )


def _name_matches(host: str, pattern: str) -> bool:
    """Match a hostname against a cert name or SAN, including wildcard rules.

    `*.example.com` matches `foo.example.com` (single label) but not
    `example.com` or `foo.bar.example.com`.
    """
    if not pattern:
        return False
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if not suffix:
            return False
        if host.endswith("." + suffix):
            label = host[: -(len(suffix) + 1)]
            return "." not in label and bool(label)
    return False


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _humanize_delta(d: timedelta) -> str:
    seconds = int(abs(d.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
