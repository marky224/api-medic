"""Tests for core.checks.http."""

from __future__ import annotations

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.checks.http import (
    REDIRECT_TOO_MANY_THRESHOLD,
    cors_misconfigured,
    headers_duplicate,
    redirect_loop,
    redirect_protocol_downgrade,
    redirect_too_many,
)
from api_medic.core.models import TimingBreakdown


def _cap(
    request_headers: dict[str, str] | None = None,
    response: CapturedResponse | None = None,
    raw_request_headers: list[tuple[str, str]] | None = None,
    redirect_chain: list[str] | None = None,
) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url="https://api.example.com/v1/users",
        headers=request_headers or {},
        raw_headers=raw_request_headers,
        body=b"",
        response=response,
        timing=TimingBreakdown(),
        redirect_chain=redirect_chain,
        source="live",
    )


def _resp(
    status: int = 200,
    headers: dict[str, str] | None = None,
    raw_headers: list[tuple[str, str]] | None = None,
) -> CapturedResponse:
    return CapturedResponse(
        status_code=status,
        status_text="",
        headers=headers or {},
        raw_headers=raw_headers,
        body=b"",
        protocol="HTTP/1.1",
    )


class TestCorsMisconfigured:
    def test_no_origin_header_skipped(self):
        cap = _cap(response=_resp(headers={"Access-Control-Allow-Origin": "*"}))
        assert cors_misconfigured(cap) is None

    def test_no_response_skipped(self):
        cap = _cap(request_headers={"Origin": "https://app.example.org"})
        assert cors_misconfigured(cap) is None

    def test_wildcard_allow_origin_not_flagged(self):
        cap = _cap(
            request_headers={"Origin": "https://app.example.org"},
            response=_resp(headers={"Access-Control-Allow-Origin": "*"}),
        )
        assert cors_misconfigured(cap) is None

    def test_exact_match_not_flagged(self):
        cap = _cap(
            request_headers={"Origin": "https://app.example.org"},
            response=_resp(headers={"Access-Control-Allow-Origin": "https://app.example.org"}),
        )
        assert cors_misconfigured(cap) is None

    def test_origin_mismatch_flagged(self):
        cap = _cap(
            request_headers={"Origin": "https://app.example.org"},
            response=_resp(headers={"Access-Control-Allow-Origin": "https://app.example.com"}),
        )
        finding = cors_misconfigured(cap)
        assert finding is not None
        assert finding.id == "http.cors.misconfigured"
        assert finding.severity == "critical"
        assert finding.evidence == {
            "request_origin": "https://app.example.org",
            "allow_origin": "https://app.example.com",
            "match": False,
        }

    def test_missing_allow_origin_flagged(self):
        cap = _cap(
            request_headers={"Origin": "https://app.example.org"},
            response=_resp(headers={}),
        )
        finding = cors_misconfigured(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert finding.evidence["allow_origin"] is None


class TestHeadersDuplicate:
    def test_no_raw_headers_skipped(self):
        # Phase 3a runner doesn't populate raw_headers — check should no-op.
        cap = _cap(response=_resp())
        assert headers_duplicate(cap) is None

    def test_unique_headers_not_flagged(self):
        cap = _cap(
            response=_resp(
                raw_headers=[
                    ("Content-Type", "application/json"),
                    ("Content-Length", "10"),
                ]
            )
        )
        assert headers_duplicate(cap) is None

    def test_same_value_duplicates_not_flagged(self):
        # Two Set-Cookie with the same value is harmless — don't surface.
        cap = _cap(
            response=_resp(
                raw_headers=[
                    ("X-Custom", "foo"),
                    ("X-Custom", "foo"),
                ]
            )
        )
        assert headers_duplicate(cap) is None

    def test_conflicting_response_dup_flagged(self):
        cap = _cap(
            response=_resp(
                raw_headers=[
                    ("Cache-Control", "no-store"),
                    ("Cache-Control", "max-age=3600"),
                ]
            )
        )
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.id == "http.headers.duplicate"
        assert finding.severity == "warning"
        ev = finding.evidence
        assert ev is not None
        dup = ev["duplicates"][0]
        assert dup["location"] == "response"
        assert dup["name"] == "Cache-Control"
        assert dup["values"] == ["no-store", "max-age=3600"]

    def test_request_dup_flagged(self):
        cap = _cap(
            raw_request_headers=[
                ("Authorization", "Bearer a"),
                ("Authorization", "Bearer b"),
            ],
            response=_resp(),
        )
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.evidence is not None
        assert finding.evidence["duplicates"][0]["location"] == "request"

    def test_case_insensitive_grouping(self):
        cap = _cap(
            response=_resp(
                raw_headers=[
                    ("content-type", "application/json"),
                    ("Content-Type", "text/html"),
                ]
            )
        )
        finding = headers_duplicate(cap)
        assert finding is not None


class TestRedirectLoop:
    def test_empty_chain_skipped(self):
        assert redirect_loop(_cap()) is None

    def test_short_chain_skipped(self):
        cap = _cap(redirect_chain=["https://x.com/"])
        assert redirect_loop(cap) is None

    def test_no_loop_not_flagged(self):
        cap = _cap(
            redirect_chain=[
                "https://x.com/",
                "https://x.com/login",
                "https://x.com/dashboard",
            ]
        )
        assert redirect_loop(cap) is None

    def test_loop_flagged(self):
        cap = _cap(
            redirect_chain=[
                "https://x.com/",
                "https://x.com/login",
                "https://x.com/",  # back to start
            ]
        )
        finding = redirect_loop(cap)
        assert finding is not None
        assert finding.id == "http.redirect.loop"
        assert finding.evidence is not None
        assert finding.evidence["repeated_url"] == "https://x.com/"
        assert finding.evidence["first_index"] == 0
        assert finding.evidence["second_index"] == 2


class TestRedirectTooMany:
    def test_no_chain_skipped(self):
        assert redirect_too_many(_cap()) is None

    def test_short_chain_not_flagged(self):
        # Single HTTP→HTTPS redirect — chain length 2, redirect count 1.
        cap = _cap(redirect_chain=["http://x.com/", "https://x.com/"])
        assert redirect_too_many(cap) is None

    def test_below_threshold_not_flagged(self):
        # One hop below threshold (THRESHOLD-1 redirects).
        chain = [f"https://x.com/step{i}" for i in range(REDIRECT_TOO_MANY_THRESHOLD)]
        # chain length = THRESHOLD → redirect count = THRESHOLD - 1
        assert redirect_too_many(_cap(redirect_chain=chain)) is None

    def test_at_threshold_flagged_critical(self):
        # Exactly THRESHOLD redirects — chain length = THRESHOLD + 1.
        chain = [f"https://x.com/step{i}" for i in range(REDIRECT_TOO_MANY_THRESHOLD + 1)]
        finding = redirect_too_many(_cap(redirect_chain=chain))
        assert finding is not None
        assert finding.id == "http.redirect.too_many"
        assert finding.severity == "critical"
        assert finding.evidence is not None
        assert finding.evidence["redirect_count"] == REDIRECT_TOO_MANY_THRESHOLD
        assert finding.evidence["threshold"] == REDIRECT_TOO_MANY_THRESHOLD
        assert finding.evidence["chain"] == chain

    def test_well_above_threshold_flagged(self):
        # The httpbin.org/redirect/10 case — 10 redirects, chain length 11.
        chain = [f"https://x.com/step{i}" for i in range(11)]
        finding = redirect_too_many(_cap(redirect_chain=chain))
        assert finding is not None
        assert finding.evidence is not None
        assert finding.evidence["redirect_count"] == 10
        assert "10 hops" in finding.title


class TestRedirectProtocolDowngrade:
    def test_empty_chain_skipped(self):
        assert redirect_protocol_downgrade(_cap()) is None

    def test_https_to_https_not_flagged(self):
        cap = _cap(
            redirect_chain=[
                "https://x.com/",
                "https://x.com/dashboard",
            ]
        )
        assert redirect_protocol_downgrade(cap) is None

    def test_https_to_http_flagged(self):
        cap = _cap(
            redirect_chain=[
                "https://x.com/",
                "http://x.com/dashboard",
            ]
        )
        finding = redirect_protocol_downgrade(cap)
        assert finding is not None
        assert finding.id == "http.redirect.protocol_downgrade"
        assert finding.severity == "critical"
        assert finding.evidence == {
            "from_url": "https://x.com/",
            "to_url": "http://x.com/dashboard",
            "hop_index": 0,
        }

    def test_http_to_https_not_flagged(self):
        # Upgrade is fine.
        cap = _cap(
            redirect_chain=[
                "http://x.com/",
                "https://x.com/",
            ]
        )
        assert redirect_protocol_downgrade(cap) is None
