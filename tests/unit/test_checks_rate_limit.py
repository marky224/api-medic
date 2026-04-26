"""Tests for core.checks.rate_limit."""

from __future__ import annotations

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.checks.rate_limit import rate_limit_approaching, rate_limit_hit
from api_medic.core.models import TimingBreakdown


def _cap(response: CapturedResponse | None) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url="https://api.example.com/v1/search",
        headers={},
        body=b"",
        response=response,
        timing=TimingBreakdown(),
        source="live",
    )


def _resp(status: int, headers: dict[str, str] | None = None) -> CapturedResponse:
    return CapturedResponse(
        status_code=status,
        status_text="",
        headers=headers or {},
        body=b"",
        protocol="HTTP/2",
    )


class TestRateLimitHit:
    def test_no_response(self):
        assert rate_limit_hit(_cap(None)) is None

    def test_200_not_flagged(self):
        assert rate_limit_hit(_cap(_resp(200))) is None

    def test_429_flagged_minimal_evidence(self):
        finding = rate_limit_hit(_cap(_resp(429)))
        assert finding is not None
        assert finding.id == "rate_limit.hit"
        assert finding.severity == "critical"

    def test_429_with_full_metadata(self):
        finding = rate_limit_hit(
            _cap(
                _resp(
                    429,
                    {
                        "Retry-After": "60",
                        "X-RateLimit-Limit": "1000",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1761399420",
                    },
                )
            )
        )
        assert finding is not None
        ev = finding.evidence
        assert ev is not None
        assert ev["limit"] == 1000
        assert ev["remaining"] == 0
        assert ev["retry_after_seconds"] == 60
        assert ev["reset_at"].endswith("Z")
        assert finding.suggested_fix is not None
        assert "60 seconds" in finding.suggested_fix

    def test_lowercase_headers(self):
        finding = rate_limit_hit(
            _cap(
                _resp(
                    429,
                    {
                        "retry-after": "30",
                        "x-ratelimit-limit": "500",
                        "x-ratelimit-remaining": "0",
                    },
                )
            )
        )
        assert finding is not None
        assert finding.evidence == {
            "limit": 500,
            "remaining": 0,
            "retry_after_seconds": 30,
        }

    def test_malformed_retry_after_falls_through(self):
        finding = rate_limit_hit(_cap(_resp(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})))
        assert finding is not None
        # HTTP-date form is intentionally skipped — finding still fires, just
        # without retry_after_seconds in evidence.
        assert finding.evidence is None or "retry_after_seconds" not in finding.evidence


class TestRateLimitApproaching:
    def test_no_response(self):
        assert rate_limit_approaching(_cap(None)) is None

    def test_429_skipped_in_favour_of_hit(self):
        cap = _cap(
            _resp(
                429,
                {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "5"},
            )
        )
        assert rate_limit_approaching(cap) is None

    def test_no_rate_limit_headers(self):
        assert rate_limit_approaching(_cap(_resp(200))) is None

    def test_above_threshold_not_flagged(self):
        cap = _cap(
            _resp(
                200,
                {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "500"},
            )
        )
        assert rate_limit_approaching(cap) is None

    def test_at_exact_threshold_not_flagged(self):
        # 100/1000 = 10% — boundary is not flagged (strictly less than).
        cap = _cap(
            _resp(
                200,
                {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "100"},
            )
        )
        assert rate_limit_approaching(cap) is None

    def test_below_threshold_flagged(self):
        cap = _cap(
            _resp(
                200,
                {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "50"},
            )
        )
        finding = rate_limit_approaching(cap)
        assert finding is not None
        assert finding.id == "rate_limit.approaching"
        assert finding.severity == "warning"
        assert finding.evidence == {"limit": 1000, "remaining": 50}

    def test_zero_limit_not_flagged(self):
        # Avoid division-by-zero and nonsense limit values.
        cap = _cap(
            _resp(
                200,
                {"X-RateLimit-Limit": "0", "X-RateLimit-Remaining": "0"},
            )
        )
        assert rate_limit_approaching(cap) is None
