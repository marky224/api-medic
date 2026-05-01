"""HTTP-semantic diagnostic checks.

Five checks. Three of them (`headers.duplicate` and the three `redirect.*`)
depend on data that's only populated by the Runner once Phase 3b-F lands;
they no-op gracefully when the relevant fields are unset, so existing
HAR/curl sources don't false-positive.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..captured import CapturedRequest
from ..models import Finding
from . import register

REDIRECT_TOO_MANY_THRESHOLD = 5


@register
def cors_misconfigured(captured: CapturedRequest) -> Finding | None:
    """Origin sent on the request, but Access-Control-Allow-Origin doesn't
    permit it (or is missing)."""
    origin = _find(captured.headers, "Origin")
    if not origin:
        return None
    if captured.response is None:
        return None

    allow = _find(captured.response.headers, "Access-Control-Allow-Origin")
    if allow is None:
        return Finding(
            id="http.cors.misconfigured",
            severity="critical",
            title="CORS response missing Access-Control-Allow-Origin",
            explanation=(
                f"The request was made from origin {origin}, but the response "
                "has no Access-Control-Allow-Origin header. The browser will "
                "block the response from reaching JS."
            ),
            evidence={"request_origin": origin, "allow_origin": None},
            suggested_fix=(
                f"Set Access-Control-Allow-Origin: {origin} on the server "
                "(or a wildcard if the API is fully public)."
            ),
        )

    allow = allow.strip()
    if allow == "*" or allow == origin:
        return None  # explicitly allowed

    return Finding(
        id="http.cors.misconfigured",
        severity="critical",
        title="CORS preflight does not allow this origin",
        explanation=(
            f"The browser sent a request from {origin} but the server only "
            f"allows {allow}. The browser will block the response."
        ),
        evidence={
            "request_origin": origin,
            "allow_origin": allow,
            "match": False,
        },
        suggested_fix=(
            f"Add {origin} to the server's Access-Control-Allow-Origin list, "
            "or use a proxy with the correct origin."
        ),
    )


@register
def headers_duplicate(captured: CapturedRequest) -> Finding | None:
    """Same header appears multiple times with conflicting values.

    Needs `raw_headers` populated on the request or response — the dict-shaped
    `headers` would have already collapsed duplicates. Runner adds this in
    Phase 3b-F; until then, this check no-ops on most inputs.
    """
    request_dups = _find_duplicates(captured.raw_headers)
    response_dups = (
        _find_duplicates(captured.response.raw_headers) if captured.response is not None else {}
    )

    findings: list[dict[str, Any]] = []
    for name, values in request_dups.items():
        findings.append({"location": "request", "name": name, "values": values})
    for name, values in response_dups.items():
        findings.append({"location": "response", "name": name, "values": values})

    if not findings:
        return None

    first = findings[0]
    return Finding(
        id="http.headers.duplicate",
        severity="warning",
        title="Header sent more than once with conflicting values",
        explanation=(
            f"The {first['location']} contains multiple {first['name']} "
            "headers with different values. Different proxies and servers "
            "pick different ones — usually first or last — which leads to "
            "inconsistent behaviour across hops."
        ),
        evidence={"duplicates": findings},
        suggested_fix=(
            "Send each header exactly once. If multiple values are intended, "
            "join them with a comma per RFC 9110."
        ),
    )


@register
def redirect_loop(captured: CapturedRequest) -> Finding | None:
    """A URL appears twice in the redirect chain."""
    chain = captured.redirect_chain
    if not chain or len(chain) < 2:
        return None
    seen: dict[str, int] = {}
    for i, url in enumerate(chain):
        if url in seen:
            return Finding(
                id="http.redirect.loop",
                severity="critical",
                title="Redirect chain has a cycle",
                explanation=(
                    f"The redirect chain visits {url} at positions "
                    f"{seen[url]} and {i}. Most clients will eventually give "
                    "up; some will spin until they hit a cap."
                ),
                evidence={
                    "chain": chain,
                    "repeated_url": url,
                    "first_index": seen[url],
                    "second_index": i,
                },
                suggested_fix=(
                    "Inspect the server's redirect rules — usually a "
                    "misconfigured rewrite rule or auth-flow loop."
                ),
            )
        seen[url] = i
    return None


@register
def redirect_too_many(captured: CapturedRequest) -> Finding | None:
    """The redirect chain is longer than well-configured APIs need.

    Below `REDIRECT_TOO_MANY_THRESHOLD` redirects, the occasional HTTP→HTTPS
    or canonical-host hop is normal. At or above it, the server is almost
    certainly mis-routing — or stuck in a near-loop the cycle detector
    can't see (e.g. distinct query strings on each hop).
    """
    chain = captured.redirect_chain
    if not chain:
        return None
    redirect_count = len(chain) - 1  # the last URL is the final response, not a redirect
    if redirect_count < REDIRECT_TOO_MANY_THRESHOLD:
        return None
    return Finding(
        id="http.redirect.too_many",
        severity="critical",
        title=f"Redirect chain has {redirect_count} hops",
        explanation=(
            f"The request was redirected {redirect_count} times before "
            "reaching a final response. Most well-configured APIs need at "
            "most one or two hops (HTTP→HTTPS, canonical host). Long chains "
            "add latency on every call and usually point to a stale rewrite "
            "rule, an auth flow bouncing between login and callback, or a "
            "CDN rule that never matches the origin."
        ),
        evidence={
            "redirect_count": redirect_count,
            "threshold": REDIRECT_TOO_MANY_THRESHOLD,
            "chain": chain,
        },
        suggested_fix=(
            "Walk the chain in the evidence and find the hop that's "
            "redirecting unexpectedly. The fix is usually on the server "
            "(rewrite rule, auth callback, CDN routing) — not in the client."
        ),
    )


@register
def redirect_protocol_downgrade(captured: CapturedRequest) -> Finding | None:
    """An HTTPS request was redirected to an HTTP URL."""
    chain = captured.redirect_chain
    if not chain or len(chain) < 2:
        return None
    for i in range(len(chain) - 1):
        prev = urlparse(chain[i]).scheme.lower()
        nxt = urlparse(chain[i + 1]).scheme.lower()
        if prev == "https" and nxt == "http":
            return Finding(
                id="http.redirect.protocol_downgrade",
                severity="critical",
                title="Redirect downgrades from HTTPS to HTTP",
                explanation=(
                    f"Hop {i} is HTTPS but hop {i + 1} is plain HTTP. "
                    "Modern browsers refuse this; older ones leak credentials "
                    "and cookies in plaintext over the network."
                ),
                evidence={
                    "from_url": chain[i],
                    "to_url": chain[i + 1],
                    "hop_index": i,
                },
                suggested_fix=(
                    "Update the server's redirect target to HTTPS. If the "
                    "downstream service really is HTTP-only, terminate TLS "
                    "at a proxy in front of it instead."
                ),
            )
    return None


def _find(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _find_duplicates(
    raw: list[tuple[str, str]] | None,
) -> dict[str, list[str]]:
    """Return {name (canonical-cased): [v1, v2, ...]} for headers seen more
    than once with different values. Same-value duplicates aren't surfaced —
    they're noisy but harmless."""
    if raw is None:
        return {}
    by_name: dict[str, list[str]] = {}
    canonical: dict[str, str] = {}  # lower → first-seen casing
    for name, value in raw:
        key = name.lower()
        canonical.setdefault(key, name)
        by_name.setdefault(key, []).append(value)
    out: dict[str, list[str]] = {}
    for key, values in by_name.items():
        unique_values = []
        for v in values:
            if v not in unique_values:
                unique_values.append(v)
        if len(unique_values) > 1:
            out[canonical[key]] = unique_values
    return out
