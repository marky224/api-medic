"""Tests for core.checks.auth — Phase 3a ships only jwt_expired."""

from __future__ import annotations

import base64
import itertools
import json
import re
import time
from datetime import datetime
from typing import Any

import pytest

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.checks import auth
from api_medic.core.checks.auth import (
    auth_missing,
    header_whitespace,
    jwt_expired,
    jwt_not_yet_valid,
)
from api_medic.core.engine import analyze
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

    def test_explanation_blames_the_401_only_on_a_401(self):
        past = int(time.time()) - 3 * 3600
        token = _make_jwt({"sub": "user_123", "exp": past})
        headers = {"Authorization": f"Bearer {token}"}
        blame = "most likely cause of the 401"

        on_401 = jwt_expired(_cap(headers=headers, response=_resp(401)))
        assert on_401 is not None
        assert on_401.explanation.endswith("This is the most likely cause of the 401.")

        for response in (_resp(200), _resp(403), _resp(500), None):
            finding = jwt_expired(_cap(headers=headers, response=response))
            assert finding is not None
            assert blame not in finding.explanation
            assert finding.explanation.endswith(" ago.")

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


# The clock for the claim tables below: 2027-01-15T08:00:00Z.
_NOW = 1_800_000_000


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(_NOW, tz=tz)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "datetime", _FrozenDatetime)


def _jwt_from_text(payload: str) -> str:
    """A JWT whose payload segment is `payload` exactly as written."""
    body = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signaturejunkjunkjunk"


# Times no UTC datetime can hold. Both checks skip them like a missing claim.
_OUT_OF_RANGE = {
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
    "-1e300": -1e300,
    "1e20": 1e20,
    "10000-01-01": 253402300800,
    "0000-12-31T23:59:59": -62135596801,
}
OUT_OF_RANGE_CLAIMS = [pytest.param(v, id=k) for k, v in _OUT_OF_RANGE.items()]

# (claim, (evidence, humanized time)) when the check reports the token,
# (claim, None) when it passes it.
EXP_CLAIMS = [
    pytest.param(
        _NOW - 7200,
        ({"exp": "2027-01-15T06:00:00Z", "expired_for_seconds": 7200}, "2h 0m"),
        id="2h-ago",
    ),
    pytest.param(
        _NOW - 0.5,
        ({"exp": "2027-01-15T07:59:59Z", "expired_for_seconds": 1}, "1s"),
        id="half-a-second-ago",
    ),
    pytest.param(
        0,
        ({"exp": "1970-01-01T00:00:00Z", "expired_for_seconds": _NOW}, "20833d 8h"),
        id="epoch",
    ),
    pytest.param(
        -62135596800,
        ({"exp": "0001-01-01T00:00:00Z", "expired_for_seconds": 63935596800}, "739995d 8h"),
        id="0001-01-01",
    ),
    pytest.param(_NOW, None, id="now"),
    pytest.param(_NOW + 3600, None, id="in-1h"),
    pytest.param(253402300799, None, id="9999-12-31T23:59:59"),
    *[pytest.param(v, None, id=k) for k, v in _OUT_OF_RANGE.items()],
]

NBF_CLAIMS = [
    pytest.param(
        _NOW + 1800,
        ({"nbf": "2027-01-15T08:30:00Z", "valid_in_seconds": 1800}, "30m 0s"),
        id="in-30m",
    ),
    pytest.param(
        _NOW + 0.5,
        ({"nbf": "2027-01-15T08:00:00Z", "valid_in_seconds": 0}, "0s"),
        id="in-half-a-second",
    ),
    pytest.param(
        253402300799,
        ({"nbf": "9999-12-31T23:59:59Z", "valid_in_seconds": 251602300799}, "2912063d 15h"),
        id="9999-12-31T23:59:59",
    ),
    pytest.param(_NOW, None, id="now"),
    pytest.param(_NOW - 60, None, id="1m-ago"),
    pytest.param(-62135596800, None, id="0001-01-01"),
    *[pytest.param(v, None, id=k) for k, v in _OUT_OF_RANGE.items()],
]


@pytest.mark.usefixtures("frozen_now")
class TestJwtTimeClaims:
    @pytest.mark.parametrize(("exp", "expected"), EXP_CLAIMS)
    def test_exp(self, exp: float, expected: tuple[dict[str, Any], str] | None) -> None:
        token = _make_jwt({"sub": "user_123", "exp": exp})
        finding = jwt_expired(_cap(headers={"Authorization": f"Bearer {token}"}))
        if expected is None:
            assert finding is None
            return
        evidence, ago = expected
        assert finding is not None
        assert finding.evidence == {**evidence, "sub": "user_123"}
        assert finding.explanation == f"The JWT in your Authorization header expired {ago} ago."

    @pytest.mark.parametrize(("nbf", "expected"), NBF_CLAIMS)
    def test_nbf(self, nbf: float, expected: tuple[dict[str, Any], str] | None) -> None:
        token = _make_jwt({"sub": "user_123", "nbf": nbf})
        finding = jwt_not_yet_valid(_cap(headers={"Authorization": f"Bearer {token}"}))
        if expected is None:
            assert finding is None
            return
        evidence, valid_in = expected
        assert finding is not None
        assert finding.evidence == {**evidence, "sub": "user_123"}
        assert finding.explanation.startswith(f"The JWT's `nbf` claim is {valid_in} in the future.")

    @pytest.mark.parametrize("claim", ["exp", "nbf"])
    @pytest.mark.parametrize("value", OUT_OF_RANGE_CLAIMS)
    def test_out_of_range_claim_reports_like_a_token_without_it(
        self, claim: str, value: float
    ) -> None:
        def findings(payload: dict[str, Any]) -> list[Any]:
            token = _make_jwt(payload)
            report = analyze(_cap(headers={"Authorization": f"Bearer {token}"}))
            return [f.model_dump() for f in report.findings]

        assert findings({"sub": "u", claim: value}) == findings({"sub": "u"})

    @pytest.mark.parametrize(
        ("literal", "value"),
        [
            ("NaN", "nan"),
            ("Infinity", "inf"),
            ("-Infinity", "-inf"),
            ("1e400", "inf"),
            ("-1e400", "-inf"),
        ],
    )
    def test_payload_decoder_reads_these_literals_as_floats(self, literal: str, value: str) -> None:
        payload = auth._decode_jwt_payload(_jwt_from_text(f'{{"exp": {literal}}}'))
        assert payload is not None
        assert repr(payload["exp"]) == value

    @pytest.mark.parametrize("literal", ["nan", "inf", "+Infinity"])
    def test_payload_decoder_rejects_other_spellings(self, literal: str) -> None:
        assert auth._decode_jwt_payload(_jwt_from_text(f'{{"exp": {literal}}}')) is None


# The lazy pattern auth.py used before the fix, kept to prove the new one
# finds the same token.
_OLD_BEARER_RE = re.compile(r"^\s*[Bb]earer\s+(.+?)\s*$", re.DOTALL)


def _old_token(value: str) -> str | None:
    m = _OLD_BEARER_RE.match(value)
    return m.group(1) if m else None


BEARER_TOKENS = [
    ("Bearer abc", "abc"),
    ("bearer abc", "abc"),
    ("BEARER abc", None),
    ("Bearer", None),
    ("Bearer ", None),
    ("Bearer  ", " "),
    ("Bearer \t\n", "\n"),
    ("\t\n Bearer\t\nabc\t\n ", "abc"),
    ("Bearer abc\r\n", "abc"),
    ("Bearer a\r\nb\r\n", "a\r\nb"),
    ("Bearer a b\tc ", "a b\tc"),
    ("Bearer a" + " " * 50 + "b", "a" + " " * 50 + "b"),
    # Python's \s includes \x1c-\x1f, \x85 and Unicode spaces, not \ufeff.
    ("\x1cBearer\x85abc\x1f\x85", "abc"),
    ("Bearer\u3000abc\u2028\u00a0", "abc"),
    ("Bearer abc\ufeff", "abc\ufeff"),
    ("\ufeffBearer abc", None),
    ("Bearerabc", None),
    ("x Bearer abc", None),
    ("Bearer \x00", "\x00"),
    ("", None),
]


class TestBearerToken:
    @pytest.mark.parametrize(("value", "token"), BEARER_TOKENS)
    def test_same_token_as_the_lazy_pattern(self, value: str, token: str | None) -> None:
        assert _old_token(value) == token
        assert auth._bearer_token(value) == token

    def test_same_token_as_the_lazy_pattern_on_every_short_combination(self):
        words = ["Bearer", "bearer", "a", "."]
        whitespace = [" ", "\t", "\r\n", "\x1c", "\x85", "\u3000", "\ufeff"]
        for n in range(5):
            for combo in itertools.product(words + whitespace, repeat=n):
                value = "".join(combo)
                assert auth._bearer_token(value) == _old_token(value), repr(value)

    @pytest.mark.parametrize("check", [jwt_expired, jwt_not_yet_valid])
    def test_long_whitespace_run_inside_the_token_takes_under_a_second(self, check):
        # The lazy pattern retried its trailing \s*$ from every space, which
        # takes minutes here; /api/analyze runs these checks on any HAR.
        cap = _cap(headers={"Authorization": "Bearer a" + " " * 200_000 + "b"})
        started = time.perf_counter()
        assert check(cap) is None
        assert time.perf_counter() - started < 1


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
