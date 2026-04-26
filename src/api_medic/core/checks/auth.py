"""Auth-related diagnostic checks.

Phase 3a ships only `auth.jwt.expired`. The other auth checks
(`auth.missing`, `auth.jwt.not_yet_valid`, `auth.header.whitespace`) land
in Phase 3b alongside the rest of the 18-check battery.

We deliberately don't verify the JWT signature — we just decode the payload
to read the `exp` claim. The architecture spec calls this out: signature
verification needs the server's secret/public key, which the user doesn't
have when triaging from the client side.
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
