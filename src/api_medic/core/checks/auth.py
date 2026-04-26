"""Auth-related diagnostic checks.

We deliberately don't verify JWT signatures — we just decode payloads to
read claims. The architecture spec calls this out: signature verification
needs the server's secret/public key, which the user doesn't have when
triaging from the client side.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..captured import CapturedRequest
from ..models import Finding
from . import register

_BEARER_RE = re.compile(r"^\s*[Bb]earer\s+(.+?)\s*$", re.DOTALL)


@register
def jwt_expired(captured: CapturedRequest) -> Finding | None:
    """Decode the bearer JWT and report when `exp` is in the past."""
    auth = _find_header(captured.headers, "Authorization")
    if not auth:
        return None

    m = _BEARER_RE.match(auth)
    if not m:
        return None

    token = m.group(1)
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None  # Not a JWT (opaque token, malformed, etc) — silent skip.

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None  # No exp claim — non-expiring tokens are valid.

    now = int(datetime.now(tz=timezone.utc).timestamp())
    if exp >= now:
        return None  # Still valid.

    expired_for = now - int(exp)
    exp_iso = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    evidence: dict[str, Any] = {
        "exp": exp_iso,
        "expired_for_seconds": expired_for,
    }
    sub = payload.get("sub")
    if isinstance(sub, str) and sub:
        evidence["sub"] = sub

    return Finding(
        id="auth.jwt.expired",
        severity="critical",
        title="Bearer token has expired",
        explanation=(
            f"The JWT in your Authorization header expired "
            f"{_humanize_seconds(expired_for)} ago. "
            "This is the most likely cause of the 401."
        ),
        evidence=evidence,
        suggested_fix="Refresh the token at your token endpoint and retry.",
    )


@register
def jwt_not_yet_valid(captured: CapturedRequest) -> Finding | None:
    """Decode the bearer JWT and report when `nbf` is still in the future."""
    auth = _find_header(captured.headers, "Authorization")
    if not auth:
        return None
    m = _BEARER_RE.match(auth)
    if not m:
        return None
    payload = _decode_jwt_payload(m.group(1))
    if payload is None:
        return None
    nbf = payload.get("nbf")
    if not isinstance(nbf, (int, float)):
        return None

    now = int(datetime.now(tz=timezone.utc).timestamp())
    if nbf <= now:
        return None

    valid_in = int(nbf) - now
    nbf_iso = datetime.fromtimestamp(int(nbf), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    evidence: dict[str, Any] = {"nbf": nbf_iso, "valid_in_seconds": valid_in}
    sub = payload.get("sub")
    if isinstance(sub, str) and sub:
        evidence["sub"] = sub

    return Finding(
        id="auth.jwt.not_yet_valid",
        severity="critical",
        title="Bearer token is not yet valid",
        explanation=(
            f"The JWT's `nbf` claim is {_humanize_seconds(valid_in)} in the "
            "future. The server will reject it until then. Likely a clock-skew "
            "issue between the token issuer and the local machine."
        ),
        evidence=evidence,
        suggested_fix=(
            "Check the system clocks on the issuer and the client. "
            "If skew is small, wait it out; otherwise sync NTP."
        ),
    )


@register
def auth_missing(captured: CapturedRequest) -> Finding | None:
    """Fires on a 401 with no Authorization header sent."""
    if captured.response is None or captured.response.status_code != 401:
        return None
    if _find_header(captured.headers, "Authorization") is not None:
        return None

    return Finding(
        id="auth.missing",
        severity="critical",
        title="No Authorization header sent",
        explanation=(
            "The server returned 401 and the request didn't include an "
            "Authorization header. Likely the credentials weren't attached, "
            "or were attached to the wrong header."
        ),
        evidence={"status_code": 401, "had_authorization_header": False},
        suggested_fix=(
            "Add an Authorization header (Bearer, Basic, or whatever the API expects) and retry."
        ),
    )


# Whitespace at the very edge of the value, plus any embedded newlines or
# carriage returns anywhere — these are the patterns that bite.
_EDGE_WHITESPACE_RE = re.compile(r"^\s|\s$")
_EMBEDDED_NEWLINE_RE = re.compile(r"[\r\n]")


@register
def header_whitespace(captured: CapturedRequest) -> Finding | None:
    """Fires when the Authorization value has stray whitespace or newlines."""
    auth = _find_header(captured.headers, "Authorization")
    if auth is None:
        return None

    has_edge = bool(_EDGE_WHITESPACE_RE.search(auth))
    has_embedded_nl = bool(_EMBEDDED_NEWLINE_RE.search(auth))
    if not (has_edge or has_embedded_nl):
        return None

    trailing_chars = _trailing_whitespace_chars(auth)
    leading_chars = _leading_whitespace_chars(auth)

    # Repr-style display so newlines/tabs are visible. Cap length so a long
    # JWT doesn't blow out the report.
    display = repr(auth)[1:-1]  # strip surrounding quotes
    if len(display) > 200:
        display = display[:100] + "..." + display[-50:]

    evidence: dict[str, Any] = {"header_value_repr": display}
    if leading_chars:
        evidence["leading_chars"] = leading_chars
    if trailing_chars:
        evidence["trailing_chars"] = trailing_chars
    if has_embedded_nl:
        evidence["embedded_newline"] = True

    return Finding(
        id="auth.header.whitespace",
        severity="critical",
        title="Authorization header has stray whitespace",
        explanation=(
            "The Authorization value has whitespace or a newline at the edge "
            "(or embedded). Some servers reject the header as malformed; "
            "others silently drop it. Common cause: copy-pasting a token "
            "with a trailing newline."
        ),
        evidence=evidence,
        suggested_fix="Trim the value before setting the header.",
    )


def _leading_whitespace_chars(s: str) -> list[str]:
    out: list[str] = []
    for ch in s:
        if ch.isspace():
            out.append(repr(ch)[1:-1])
        else:
            break
    return out


def _trailing_whitespace_chars(s: str) -> list[str]:
    out: list[str] = []
    for ch in reversed(s):
        if ch.isspace():
            out.append(repr(ch)[1:-1])
        else:
            break
    return list(reversed(out))


def _find_header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode the middle segment of a JWT without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    pad = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
    except (binascii.Error, ValueError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _humanize_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"
