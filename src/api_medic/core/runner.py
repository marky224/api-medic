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

import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import httpx
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

from .. import __version__
from .captured import CapturedDns, CapturedRequest, CapturedResponse, CapturedTls
from .models import TimingBreakdown
from .runner_safety import (
    InvalidRequestError,
    InvalidURLError,
    PinnedTransport,
    UnusableResponseError,
    connect_pinned,
    validate_url,
)

DEFAULT_USER_AGENT = f"api-medic/{__version__} (+https://api-medic.markandrewmarquez.com)"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0

# Redirect cap when `url_guard` is set (the hosted-demo path). A chain
# that hits the cap still trips `http.redirect.too_many`, whose threshold
# is also 5.
MAX_GUARDED_REDIRECTS = 5

# Encodings httpx transparently decompresses on response.content. Used to
# decide when to drop Content-Encoding from the captured headers so checks
# don't see a body/header inconsistency we created ourselves.
_HTTPX_DECODED_ENCODINGS = frozenset({"gzip", "deflate", "br"})

# What h11, which writes httpx's HTTP/1.1 requests, won't send: a method or
# header name that isn't an RFC 9110 token, and a header value holding NUL,
# LF, VT, FF or CR, or starting or ending with a space or tab. httpx raises
# LocalProtocolError for these once the connection is open, which run()
# would report as no response.
_TOKEN = re.compile(r"[-!#$%&'*+.^_`|~0-9A-Za-z]+")
_TOKEN_CHARACTERS = "letters, digits and !#$%&'*+-.^_`|~"
_NOT_IN_A_HEADER_VALUE = re.compile(r"[\x00\n\x0b\x0c\r]")
# A Content-Length h11 takes: once its comma-separated parts are stripped
# and merged, one number of 1 to 20 ASCII digits. h11 strips each part with
# bytes.strip(), which also strips the characters _NOT_IN_A_HEADER_VALUE
# refuses, but not \x1c-\x1f as str.strip() does.
_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")


def run(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    body: bytes | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = True,
    transport: httpx.BaseTransport | None = None,
    probe_network: bool = True,
    url_guard: Callable[[str], object] | None = None,
    pin_connections: bool = False,
) -> CapturedRequest:
    """Execute an HTTP request and capture the result.

    `transport` is for tests (pass an `httpx.MockTransport`); not used in
    production. `probe_network=False` skips DNS + TLS pre-flight probes —
    unit tests pass it to stay offline; production leaves it on.

    `url_guard` is for the public Lambda. When set, redirects are followed
    by hand instead of by httpx: each hop's target is passed to the guard
    before it is fetched, and whatever the guard raises propagates to the
    caller. At most MAX_GUARDED_REDIRECTS hops are followed; past that the
    last redirect response is returned unfollowed. The caller is still
    responsible for vetting the initial URL.

    `pin_connections` is for the public Lambda too. Every connection (the
    TLS probe, the request and each redirect hop) then resolves the name
    once, refuses any non-public address with UnsafeURLError, and
    connects to the checked IP, so a DNS answer that changes after the
    caller's check can't redirect it. Ignored for the request when
    `transport` is given.

    Network failures (DNS, connection refused, timeout) produce a
    CapturedRequest with `response=None`, and so does a redirect whose
    Location httpx can't turn into a request or whose URL validate_url
    rejects (with `url_guard` set, the guard refuses such a hop first).
    Diagnostic checks that need a response no-op for it. A request that can't be sent as given raises
    InvalidRequestError (see validate_request) before anything is sent. A
    final response whose status is outside 100-599, which h11 accepts but a
    Report can't hold, raises UnusableResponseError after the exchange.

    `headers` is a mapping or a list of (name, value) pairs, such as a
    curl command's -H headers; validate_request refuses a name given
    twice, so the request and its report hold each once. A Host header is
    sent as given; the connection still goes to the URL's host.
    api-medic's User-Agent is added unless one is given, in any case. A
    request that gives a Transfer-Encoding goes without the Content-Length
    httpx would add beside it, as curl sends it, and h11 chunk-encodes the
    body.
    """
    validate_request(method, url, headers, body)
    request_headers = dict(headers or {})
    if not any(name.lower() == "user-agent" for name in request_headers):
        request_headers["User-Agent"] = DEFAULT_USER_AGENT

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
            if pin_connections:
                captured_tls, tls_ms = _probe_tls(parsed.hostname, port, connect=connect_pinned)
            else:
                captured_tls, tls_ms = _probe_tls(parsed.hostname, port)

    if transport is None and pin_connections:
        transport = PinnedTransport()

    start = time.monotonic()
    response: httpx.Response | None = None
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=follow_redirects and url_guard is None,
            transport=transport,
            event_hooks={"request": [_refuse_unsendable_hop]},
        ) as client:
            request = client.build_request(
                method=method,
                url=url,
                headers=request_headers,
                content=body_bytes if body_bytes else None,
            )
            if any(name.lower() == "transfer-encoding" for name in request_headers):
                # httpx adds a Content-Length for the body beside it, which
                # RFC 9112 forbids a sender to send; validate_request has
                # refused one the caller gave. A request without a body has
                # none, hence pop() and not del.
                request.headers.pop("Content-Length", None)
            response = client.send(request)
            if follow_redirects and url_guard is not None:
                response = _follow_guarded_redirects(client, response, url_guard)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, _UnsendableHopError):
        # Beyond network and protocol errors: httpx builds the next request
        # as soon as a redirect arrives, and raises InvalidURL or
        # UnicodeError for a Location it can't use (e.g. "https:host", or an
        # invalid xn-- label); _refuse_unsendable_hop stops one validate_url
        # rejects. Treat both as httpx treats a Location it can't parse:
        # the exchange failed. The submitted request was validated up
        # front, so these can only come from the response.
        response = None
    total_ms = (time.monotonic() - start) * 1000.0
    if response is not None and not 100 <= response.status_code <= 599:
        raise UnusableResponseError(
            f"The server answered with status {response.status_code}, outside 100-599."
        )

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


def validate_request(
    method: str,
    url: str,
    headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    body: bytes | str | None = None,
) -> None:
    """Raise InvalidRequestError if httpx can't send this request as given.

    The URL goes through validate_url (InvalidURLError); the method and
    header names and values must be ASCII, the method and each header name
    an RFC 9110 token, and each header value free of NUL, LF, VT, FF and CR
    and of a space or tab at either end, which h11, httpx's HTTP/1.1
    writer, refuses. `headers` is a mapping or a list of (name, value)
    pairs, and no name may be given twice, compared case-insensitively,
    even with the same value: of two in one spelling a mapping keeps only
    the last, and h11 refuses two Host or Transfer-Encoding headers or two
    different lengths. A str body must encode as UTF-8. Then the body's
    framing, which h11 also refuses or can't send: a Content-Length must be
    one whole number of 1 to 20 digits once its comma-separated parts are
    merged, and match the body's length in bytes, and a Transfer-Encoding
    must be chunked, in any case. Content-Length and Transfer-Encoding
    can't both be given, which h11 would send (RFC 9112 forbids it). run()
    calls this first. A caller with work to do before run() (the Lambda's
    SSRF guard) can call it earlier, so bad input gets a 400 before any DNS
    lookup.
    """
    validate_url(url)
    if not method.isascii():
        raise InvalidRequestError(f"Method must be ASCII (got {method!r}).")
    if not _TOKEN.fullmatch(method):
        raise InvalidRequestError(f"Method can only hold {_TOKEN_CHARACTERS} (got {method!r}).")
    pairs = list(headers.items()) if isinstance(headers, Mapping) else list(headers or [])
    for name, value in pairs:
        if not (name.isascii() and value.isascii()):
            raise InvalidRequestError(f"Header names and values must be ASCII (header {name!r}).")
        if not _TOKEN.fullmatch(name):
            raise InvalidRequestError(
                f"Header names can only hold {_TOKEN_CHARACTERS} (header {name!r})."
            )
        if held := _NOT_IN_A_HEADER_VALUE.search(value):
            raise InvalidRequestError(f"Header values can't hold {held[0]!r} (header {name!r}).")
        if value[:1] in (" ", "\t") or value[-1:] in (" ", "\t"):
            raise InvalidRequestError(
                f"Header values can't start or end with a space or tab (header {name!r})."
            )
    names: set[str] = set()
    for name, _ in pairs:
        if name.lower() in names:
            raise InvalidRequestError(
                f"Header {name!r} is given more than once (names are case-insensitive, "
                "and a repeat with the same value counts too); give it once."
            )
        names.add(name.lower())
    if isinstance(body, str):
        try:
            body = body.encode("utf-8")
        except UnicodeEncodeError as e:
            raise InvalidRequestError(
                "Body can't be encoded as UTF-8 (it contains an unpaired surrogate)."
            ) from e
    _validate_framing(pairs, len(body or b""))


def _validate_framing(headers: list[tuple[str, str]], body_size: int) -> None:
    """validate_request's rules for Content-Length and Transfer-Encoding, on
    headers that passed its character rules (so they are ASCII), for a body
    of `body_size` bytes. The digits are counted before int() reads them:
    int() raises ValueError past 4,300 digits."""
    lengths: list[tuple[str, int]] = []
    coded = False
    for name, value in headers:
        if name.lower() == "content-length":
            parts = {part.strip(" \t") for part in value.split(",")}
            number = parts.pop() if len(parts) == 1 else ""
            if not _CONTENT_LENGTH.fullmatch(number):
                raise InvalidRequestError(
                    "Content-Length must be one whole number of bytes, 1 to 20 digits "
                    f"(header {name!r})."
                )
            lengths.append((name, int(number)))
        elif name.lower() == "transfer-encoding":
            if value.lower() != "chunked":
                raise InvalidRequestError(
                    f"Transfer-Encoding can only be chunked (header {name!r})."
                )
            coded = True
    if lengths and coded:
        raise InvalidRequestError(
            "Content-Length and Transfer-Encoding can't be sent together; leave Content-Length out."
        )
    for name, length in lengths:
        if length != body_size:
            raise InvalidRequestError(
                f"Content-Length is {length} but the body is {body_size} "
                f"byte{'' if body_size == 1 else 's'} (header {name!r})."
            )


class _UnsendableHopError(Exception):
    """A redirect hop whose URL validate_url rejects; it isn't sent."""


def _refuse_unsendable_hop(request: httpx.Request) -> None:
    """httpx request hook: don't send a redirect hop validate_url rejects.

    httpx doesn't range-check a Location's port, and getaddrinfo wraps one
    that's too big (99999 becomes 34463), so without this a hop to
    https://example.com:99999/ would go to another port. The submitted URL
    has already passed validate_request, so only a hop can fail here.
    """
    try:
        validate_url(str(request.url))
    except InvalidURLError as e:
        raise _UnsendableHopError(str(e)) from e


def _follow_guarded_redirects(
    client: httpx.Client,
    response: httpx.Response,
    url_guard: Callable[[str], object],
) -> httpx.Response:
    """Follow redirects one hop at a time, guarding each target first.

    With follow_redirects off, httpx still builds the next request (method
    rewrite, header stripping, cookies) and parks it on `next_request`, so
    each hop is sent exactly as auto-follow would send it. `history` is set
    the way auto-follow sets it, so `_build_redirect_chain` sees the same
    chain.

    `auth=httpx.Auth()` (a no-op) matters: by default `send()` turns
    user:pass@ in the hop URL into Basic auth, which auto-follow never does.
    """
    history: list[httpx.Response] = []
    while response.next_request is not None and len(history) < MAX_GUARDED_REDIRECTS:
        next_request = response.next_request
        url_guard(str(next_request.url))
        history.append(response)
        response = client.send(next_request, auth=httpx.Auth())
    response.history = history
    return response


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
    host: str,
    port: int,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    connect: Callable[[str, int, float], socket.socket] | None = None,
) -> tuple[CapturedTls | None, float]:
    """Open a separate TLS connection just to inspect the cert + protocol.

    Verification is disabled — we want to *detect* mismatches in checks,
    not error out before reaching them. Returns (None, elapsed) on any
    socket/TLS failure. `connect`, if given, opens the TCP connection
    instead of socket.create_connection (the pinned, guarded path); any
    UnsafeURLError it raises propagates.
    """
    start = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = (
            socket.create_connection((host, port), timeout=timeout)
            if connect is None
            else connect(host, port, timeout)
        )
        with sock, ctx.wrap_socket(sock, server_hostname=host) as ssock:
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
