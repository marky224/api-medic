"""Live HTTP request execution.

Used by:
  * the CLI's `run` and bare-URL forms
  * the local web UI's POST /api/run
  * the hosted-demo Lambda's POST /api/run (gated by core.runner_safety)

Phase 3b adds DNS resolution (via dnspython) and a separate TLS probe (via
ssl + cryptography) on top of the httpx HTTP exchange. Those two pre-flight
operations populate `dns`, `tls`, `timing.dns_ms`, and `timing.tls_ms` —
the data the network/TLS checks consume. Connect/TTFB/download per-phase
breakdown is still aggregated into `total_ms` for now; finer slicing would
need a custom httpcore transport and is out of scope for v1.
"""

from __future__ import annotations

import socket
import ssl
import time
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import httpx
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

from .captured import CapturedDns, CapturedRequest, CapturedResponse, CapturedTls
from .models import TimingBreakdown

DEFAULT_USER_AGENT = "api-medic/0.1 (+https://api-medic.markandrewmarquez.com)"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0

# Encodings httpx transparently decompresses on response.content. Used to
# decide when to drop Content-Encoding from the captured headers so checks
# don't see a body/header inconsistency we created ourselves.
_HTTPX_DECODED_ENCODINGS = frozenset({"gzip", "deflate", "br"})


def run(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
    transport: httpx.BaseTransport | None = None,
    probe_network: bool = True,
) -> CapturedRequest:
    """Execute an HTTP request and capture the result.

    `transport` is for tests (pass an `httpx.MockTransport`); not used in
    production. `probe_network=False` skips DNS + TLS pre-flight probes —
    unit tests pass it to stay offline; production leaves it on.

    Network failures (DNS, connection refused, timeout) produce a
    CapturedRequest with `response=None`. Diagnostic checks that need a
    response no-op for it.
    """
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    if body is None:
        body_bytes = b""
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body

    parsed = urlparse(url)
    is_https = parsed.scheme.lower() == "https"

    captured_dns: CapturedDns | None = None
    captured_tls: CapturedTls | None = None
    dns_ms: float | None = None
    tls_ms: float | None = None

    if probe_network and parsed.hostname:
        captured_dns, dns_ms = _resolve_dns(parsed.hostname)
        if is_https:
            port = parsed.port or 443
            captured_tls, tls_ms = _probe_tls(parsed.hostname, port)

    start = time.monotonic()
    response: httpx.Response | None = None
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=follow_redirects,
            transport=transport,
        ) as client:
            response = client.request(
                method=method,
                url=url,
                headers=request_headers,
                content=body_bytes if body_bytes else None,
            )
    except httpx.HTTPError:
        response = None
    total_ms = (time.monotonic() - start) * 1000.0

    captured_response: CapturedResponse | None = None
    redirect_chain: list[str] | None = None
    if response is not None:
        raw_headers = [(k.decode("ascii"), v.decode("latin-1")) for k, v in response.headers.raw]

        # httpx auto-decompresses gzip/deflate/br before returning .content,
        # but leaves the Content-Encoding header reflecting the wire value.
        # Drop the header so checks (encoding_mismatch, content_length_mismatch)
        # see a captured response that's internally consistent with its body.
        wire_encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if wire_encoding in _HTTPX_DECODED_ENCODINGS:
            raw_headers = [(k, v) for k, v in raw_headers if k.lower() != "content-encoding"]

        captured_response = CapturedResponse(
            status_code=response.status_code,
            status_text=response.reason_phrase or _default_reason(response.status_code),
            headers=dict(raw_headers),
            raw_headers=raw_headers,
            body=response.content,
            protocol=response.http_version,
        )
        redirect_chain = _build_redirect_chain(response)

    return CapturedRequest(
        method=method.upper(),
        url=url,
        headers=request_headers,
        body=body_bytes,
        response=captured_response,
        timing=TimingBreakdown(dns_ms=dns_ms, tls_ms=tls_ms, total_ms=total_ms),
        redirect_chain=redirect_chain,
        dns=captured_dns,
        tls=captured_tls,
        source="live",
    )


def _resolve_dns(
    host: str, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> tuple[CapturedDns, float]:
    """Resolve A and AAAA records. Returns (CapturedDns, elapsed_ms).

    No exceptions escape — failed lookups produce CapturedDns(records=[]).
    The dns.no_records check distinguishes that from "didn't probe" via the
    presence of CapturedDns itself.
    """
    start = time.monotonic()
    records: list[str] = []
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        for rtype in ("A", "AAAA"):
            try:
                answers = resolver.resolve(host, rtype)
                records.extend(str(rdata) for rdata in answers)
            except (
                dns.resolver.NoAnswer,
                dns.resolver.NXDOMAIN,
                dns.resolver.NoNameservers,
                dns.exception.Timeout,
            ):
                pass
    except dns.exception.DNSException:
        pass
    elapsed_ms = (time.monotonic() - start) * 1000.0
    return CapturedDns(records=records), elapsed_ms


def _probe_tls(
    host: str, port: int, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> tuple[CapturedTls | None, float]:
    """Open a separate TLS connection just to inspect the cert + protocol.

    Verification is disabled — we want to *detect* mismatches in checks,
    not error out before reaching them. Returns (None, elapsed) on any
    socket/TLS failure.
    """
    start = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as ssock,
        ):
            der = ssock.getpeercert(binary_form=True)
            version = ssock.version() or ""
            if not der:
                return None, (time.monotonic() - start) * 1000.0
            cert = x509.load_der_x509_certificate(der)
            tls = CapturedTls(
                not_before=cert.not_valid_before_utc,
                not_after=cert.not_valid_after_utc,
                subject_common_name=_extract_cn(cert.subject),
                subject_alt_names=_extract_sans(cert),
                issuer_common_name=_extract_cn(cert.issuer),
                negotiated_protocol_version=version,
            )
            return tls, (time.monotonic() - start) * 1000.0
    except (OSError, ssl.SSLError):
        return None, (time.monotonic() - start) * 1000.0


def _extract_cn(name: x509.Name) -> str | None:
    try:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    except x509.AttributeNotFound:
        return None
    if not attrs:
        return None
    value = attrs[0].value
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


def _extract_sans(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return []
    san = ext.value
    if not isinstance(san, x509.SubjectAlternativeName):
        return []
    return list(san.get_values_for_type(x509.DNSName))


def _build_redirect_chain(response: httpx.Response) -> list[str] | None:
    if not response.history:
        return None
    chain = [str(r.url) for r in response.history]
    chain.append(str(response.url))
    return chain


def _default_reason(code: int) -> str:
    if code < 200:
        return "Informational"
    if code < 300:
        return "OK"
    if code < 400:
        return "Redirect"
    if code < 500:
        return "Client Error"
    return "Server Error"
