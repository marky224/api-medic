"""Tests for core.checks.auth — Phase 3a ships only jwt_expired."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.checks.auth import (
    auth_missing,
    header_whitespace,
    jwt_expired,
    jwt_not_yet_valid,
)
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


def _cap(
    headers: dict[str, str] | None = None,
    response: CapturedResponse | None = None,
) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url="https://api.example.com/v1/users",
        headers=headers or {},
        body=b"",
        response=response,
        timing=TimingBreakdown(),
        source="live",
    )


def _resp(status: int = 200, body: bytes = b"") -> CapturedResponse:
    return CapturedResponse(
        status_code=status,
        status_text="OK",
        headers={},
        body=body,
        protocol="HTTP/1.1",
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


class TestJwtNotYetValid:
    def test_no_authorization(self):
        assert jwt_not_yet_valid(_cap()) is None

    def test_jwt_without_nbf_skipped(self):
        token = _make_jwt({"sub": "u"})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})
        assert jwt_not_yet_valid(cap) is None

    def test_nbf_in_past_not_flagged(self):
        token = _make_jwt({"sub": "u", "nbf": int(time.time()) - 60})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})
        assert jwt_not_yet_valid(cap) is None

    def test_nbf_in_future_flagged(self):
        token = _make_jwt({"sub": "user_42", "nbf": int(time.time()) + 1800})
        cap = _cap(headers={"Authorization": f"Bearer {token}"})

        finding = jwt_not_yet_valid(cap)
        assert finding is not None
        assert finding.id == "auth.jwt.not_yet_valid"
        assert finding.severity == "critical"
        assert finding.evidence is not None
        assert finding.evidence["sub"] == "user_42"
        assert finding.evidence["valid_in_seconds"] == pytest.approx(1800, abs=5)
        assert finding.evidence["nbf"].endswith("Z")


class TestAuthMissing:
    def test_no_response(self):
        assert auth_missing(_cap()) is None

    def test_200_response_not_flagged(self):
        cap = _cap(response=_resp(200))
        assert auth_missing(cap) is None

    def test_401_with_authorization_not_flagged(self):
        cap = _cap(
            headers={"Authorization": "Bearer xyz"},
            response=_resp(401),
        )
        assert auth_missing(cap) is None

    def test_401_without_authorization_flagged(self):
        cap = _cap(response=_resp(401))
        finding = auth_missing(cap)
        assert finding is not None
        assert finding.id == "auth.missing"
        assert finding.severity == "critical"
        assert finding.evidence == {
            "status_code": 401,
            "had_authorization_header": False,
        }

    def test_lowercase_authorization_still_counts(self):
        cap = _cap(
            headers={"authorization": "Bearer xyz"},
            response=_resp(401),
        )
        assert auth_missing(cap) is None


class TestHeaderWhitespace:
    def test_no_authorization(self):
        assert header_whitespace(_cap()) is None

    def test_clean_authorization_not_flagged(self):
        cap = _cap(headers={"Authorization": "Bearer abc.def.ghi"})
        assert header_whitespace(cap) is None

    def test_trailing_newline_flagged(self):
        cap = _cap(headers={"Authorization": "Bearer abc.def.ghi\n"})
        finding = header_whitespace(cap)
        assert finding is not None
        assert finding.id == "auth.header.whitespace"
        assert finding.severity == "critical"
        ev = finding.evidence
        assert ev is not None
        assert ev["trailing_chars"] == ["\\n"]
        assert ev.get("embedded_newline") is True

    def test_leading_space_flagged(self):
        cap = _cap(headers={"Authorization": " Bearer abc.def.ghi"})
        finding = header_whitespace(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert finding.evidence["leading_chars"] == [" "]

    def test_embedded_carriage_return_flagged_even_without_edge_whitespace(self):
        cap = _cap(headers={"Authorization": "Bearer abc\rdef"})
        finding = header_whitespace(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert finding.evidence.get("embedded_newline") is True

    def test_long_header_value_truncated_in_evidence(self):
        long_token = "a" * 500
        cap = _cap(headers={"Authorization": f"Bearer {long_token}\n"})
        finding = header_whitespace(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert "..." in finding.evidence["header_value_repr"]
        assert len(finding.evidence["header_value_repr"]) < 250
