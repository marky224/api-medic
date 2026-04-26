"""Rate-limit diagnostic checks.

Two checks. `rate_limit.hit` fires on 429 and surfaces whatever rate-limit
metadata the server returned. `rate_limit.approaching` fires on a non-429
response when the remaining-quota header is below 10% of the limit.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..captured import CapturedRequest
from ..models import Finding
from . import register

APPROACHING_THRESHOLD = 0.10  # 10% of limit remaining


@register
def rate_limit_hit(captured: CapturedRequest) -> Finding | None:
    if captured.response is None or captured.response.status_code != 429:
        return None

    headers = captured.response.headers
    retry_after = _parse_retry_after(_find(headers, "Retry-After"))
    limit = _parse_int(_find(headers, "X-RateLimit-Limit"))
    remaining = _parse_int(_find(headers, "X-RateLimit-Remaining"))
    reset_at = _parse_unix_to_iso(_find(headers, "X-RateLimit-Reset"))

    evidence: dict[str, Any] = {}
    if limit is not None:
        evidence["limit"] = limit
    if remaining is not None:
        evidence["remaining"] = remaining
    if retry_after is not None:
        evidence["retry_after_seconds"] = retry_after
    if reset_at is not None:
        evidence["reset_at"] = reset_at

    if retry_after is not None:
        fix_prefix = f"Wait {retry_after} seconds before retrying. "
    else:
        fix_prefix = "Consult the API's rate-limit docs for the retry window. "

    return Finding(
        id="rate_limit.hit",
        severity="critical",
        title="Rate limit exceeded",
        explanation=(
            "The server returned 429. The client has consumed all of its "
            "allowed requests for the current window."
        ),
        evidence=evidence or None,
        suggested_fix=(
            fix_prefix + "Consider implementing exponential backoff and respecting "
            "the Retry-After header."
        ),
    )


@register
def rate_limit_approaching(captured: CapturedRequest) -> Finding | None:
    if captured.response is None:
        return None
    if captured.response.status_code == 429:
        return None  # rate_limit.hit owns this case.

    headers = captured.response.headers
    limit = _parse_int(_find(headers, "X-RateLimit-Limit"))
    remaining = _parse_int(_find(headers, "X-RateLimit-Remaining"))

    if limit is None or remaining is None or limit <= 0 or remaining < 0:
        return None
    if remaining / limit >= APPROACHING_THRESHOLD:
        return None

    return Finding(
        id="rate_limit.approaching",
        severity="warning",
        title="Approaching rate limit",
        explanation=(
            f"X-RateLimit-Remaining is {remaining} of {limit} "
            f"({remaining / limit * 100:.0f}%). At this rate you'll hit 429 soon."
        ),
        evidence={"limit": limit, "remaining": remaining},
        suggested_fix="Slow down or batch requests to stay within the limit.",
    )


def _find(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _parse_int(v: str | None) -> int | None:
    if v is None:
        return None
    s = v.strip()
    if not re.fullmatch(r"-?\d+", s):
        return None
    return int(s)


def _parse_retry_after(v: str | None) -> int | None:
    """Retry-After can be delta-seconds or HTTP-date.

    Servers overwhelmingly use delta-seconds in practice. We surface the
    integer form and skip the HTTP-date case for v1.
    """
    if v is None:
        return None
    s = v.strip()
    return int(s) if s.isdigit() else None


def _parse_unix_to_iso(v: str | None) -> str | None:
    """X-RateLimit-Reset is conventionally Unix epoch seconds."""
    n = _parse_int(v)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
