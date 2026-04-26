"""Tests for core.checks.auth — Phase 3a ships only jwt_expired."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from api_medic.core.captured import CapturedRequest
from api_medic.core.checks.auth import jwt_expired
from api_medic.core.models import TimingBreakdown


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build an unsigned JWT-shaped token (signature is junk — we don't verify)."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    body = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    return f"{header}.{body}.signaturejunkjunkjunk"


def _cap(headers: dict[str, str] | None = None) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url="https://api.example.com/v1/users",
        headers=headers or {},
        body=b"",
        response=None,
        timing=TimingBreakdown(),
        source="live",
    )


class TestJwtExpired:
    def test_no_authorization_header(self):
        assert jwt_expired(_cap()) is None

    def test_basic_auth_is_skipped(self):
        cap = _cap(headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert jwt_expired(cap) is None

    def test_opaque_bearer_token_is_skipped(self):
        cap = _cap(headers={"Authorization": "Bearer not-a-jwt"})
        assert jwt_expired(cap) is None

    def test_bearer_with_two_segments_is_skipped(self):
        cap = _cap(headers={"Authorization": "Bearer foo.bar"})
        assert jwt_expired(cap) is None

    def test_jwt_without_exp_claim_is_skipped(self):
        token = _make_jwt({"sub": "user_123"})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})
        assert jwt_expired(cap) is None

    def test_jwt_with_future_exp_is_not_flagged(self):
        future = int(time.time()) + 3600
        token = _make_jwt({"sub": "user_123", "exp": future})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})
        assert jwt_expired(cap) is None

    def test_expired_jwt_produces_critical_finding(self):
        past = int(time.time()) - 3 * 3600
        token = _make_jwt({"sub": "user_123", "exp": past})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None
        assert finding.id == "auth.jwt.expired"
        assert finding.severity == "critical"
        assert finding.title == "Bearer token has expired"
        assert finding.suggested_fix is not None

    def test_evidence_carries_exp_iso_and_expired_for_seconds_and_sub(self):
        past = int(time.time()) - 7200  # 2 hours
        token = _make_jwt({"sub": "user_123", "exp": past})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None
        ev = finding.evidence
        assert ev is not None
        assert ev["sub"] == "user_123"
        assert ev["expired_for_seconds"] == pytest.approx(7200, abs=5)
        assert ev["exp"].endswith("Z")
        assert "T" in ev["exp"]  # ISO 8601

    def test_evidence_omits_sub_when_missing(self):
        past = int(time.time()) - 60
        token = _make_jwt({"exp": past})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert "sub" not in finding.evidence

    def test_lowercase_authorization_header_works(self):
        past = int(time.time()) - 60
        token = _make_jwt({"sub": "u", "exp": past})
        cap = _cap(headers={"authorization": f"Bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None

    def test_lowercase_bearer_scheme_works(self):
        past = int(time.time()) - 60
        token = _make_jwt({"sub": "u", "exp": past})
        cap = _cap(headers={"Authorization": f"bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None

    def test_trailing_whitespace_in_header_does_not_break_parsing(self):
        past = int(time.time()) - 60
        token = _make_jwt({"sub": "u", "exp": past})
        cap = _cap(headers={"Authorization": f"Bearer {token}\n"})

        finding = jwt_expired(cap)
        assert finding is not None

    def test_humanize_format_in_explanation(self):
        past = int(time.time()) - 3 * 3600  # 3 hours
        token = _make_jwt({"sub": "u", "exp": past})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})

        finding = jwt_expired(cap)
        assert finding is not None
        assert "h" in finding.explanation  # "3h ..." in "expired 3h Xm ago"
