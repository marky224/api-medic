"""Tests for core.checks.http."""

from __future__ import annotations

import pytest

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


# The response fields RFC 9110, RFC 9111 (Cache-Control) and RFC 8288 (Link)
# define as comma-separated lists, each with two values a server may send on
# two lines.
_LIST_VALUED_RESPONSE_HEADERS = [
    ("Accept-Ranges", "bytes", "none"),
    ("Allow", "GET", "HEAD"),
    ("Authentication-Info", 'nextnonce="abc"', "nc=00000001"),
    ("Cache-Control", "no-store", "max-age=3600"),
    ("Connection", "keep-alive", "Upgrade"),
    ("Content-Encoding", "gzip", "br"),
    ("Content-Language", "en", "de"),
    ("Link", '</page/2>; rel="next"', '</page/9>; rel="last"'),
    ("Proxy-Authenticate", 'Basic realm="proxy"', "Negotiate"),
    ("Proxy-Authentication-Info", 'nextnonce="abc"', "nc=00000001"),
    ("Trailer", "Expires", "ETag"),
    ("Upgrade", "h2c", "websocket"),
    ("Vary", "Accept", "Accept-Encoding"),
    ("Via", "1.1 edge", "1.1 origin"),
    ("WWW-Authenticate", 'Basic realm="api"', "Negotiate"),
]


class TestHeadersDuplicate:
    def test_no_raw_headers_skipped(self):
        # Nothing filled raw_headers: the Runner fills a response's on a live
        # run and parse_curl a request's, and a HAR has neither. The check
        # should no-op.
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
        # The same value twice is harmless: don't surface it.
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
                    ("Content-Type", "application/json"),
                    ("Content-Type", "text/html"),
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
        assert dup["name"] == "Content-Type"
        assert dup["values"] == ["application/json", "text/html"]

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

    @pytest.mark.parametrize(
        "raw_headers",
        [
            [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")],
            [("Set-Cookie", "a=1"), ("set-cookie", "b=2")],
            [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Set-Cookie", "c=3")],
        ],
        ids=["two", "two-cases", "three"],
    )
    def test_set_cookie_response_headers_are_not_flagged(
        self, raw_headers: list[tuple[str, str]]
    ) -> None:
        # A server sets each cookie in a Set-Cookie header of its own, and
        # they can't be joined into one (RFC 9110, section 5.3).
        cap = _cap(response=_resp(raw_headers=raw_headers))
        assert headers_duplicate(cap) is None

    def test_set_cookie_is_left_out_of_another_headers_finding(self) -> None:
        cap = _cap(
            response=_resp(
                raw_headers=[
                    ("Set-Cookie", "a=1"),
                    ("Content-Type", "application/json"),
                    ("Set-Cookie", "b=2"),
                    ("Content-Type", "text/html"),
                ]
            )
        )
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.evidence == {
            "duplicates": [
                {
                    "location": "response",
                    "name": "Content-Type",
                    "values": ["application/json", "text/html"],
                }
            ]
        }
        assert "multiple Content-Type headers" in finding.explanation

    @pytest.mark.parametrize(
        ("name", "first", "second"),
        _LIST_VALUED_RESPONSE_HEADERS,
        ids=[name for name, _, _ in _LIST_VALUED_RESPONSE_HEADERS],
    )
    def test_list_valued_response_headers_are_not_flagged(
        self, name: str, first: str, second: str
    ) -> None:
        # A server may send a comma-separated list on several lines, and a
        # recipient may join them (RFC 9110, section 5.3).
        cap = _cap(response=_resp(raw_headers=[(name, first), (name, second)]))
        assert headers_duplicate(cap) is None

    def test_set_cookie_given_twice_in_a_request_is_still_flagged(self) -> None:
        # Only a response may hold several: a live run refuses a request
        # that gives a name twice.
        cap = _cap(raw_request_headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.evidence == {
            "duplicates": [{"location": "request", "name": "Set-Cookie", "values": ["a=1", "b=2"]}]
        }

    def test_list_valued_request_header_is_still_flagged(self) -> None:
        cap = _cap(raw_request_headers=[("Accept", "text/html"), ("Accept", "application/json")])
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.evidence == {
            "duplicates": [
                {
                    "location": "request",
                    "name": "Accept",
                    "values": ["text/html", "application/json"],
                }
            ]
        }

    @pytest.mark.parametrize(
        ("name", "first", "second"),
        [
            ("Content-Type", "application/json", "text/html"),
            ("Access-Control-Allow-Origin", "https://a.example", "https://b.example"),
            ("X-Custom", "foo", "bar"),
        ],
        ids=["Content-Type", "Access-Control-Allow-Origin", "X-Custom"],
    )
    def test_other_repeated_response_headers_are_still_flagged(
        self, name: str, first: str, second: str
    ) -> None:
        # A header that takes one value, or one not checked against the
        # RFCs, is still named.
        cap = _cap(response=_resp(raw_headers=[(name, first), (name, second)]))
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.evidence == {
            "duplicates": [{"location": "response", "name": name, "values": [first, second]}]
        }

    @pytest.mark.parametrize(
        ("location", "name", "first", "second"),
        [
            ("request", "Authorization", "Bearer one", "Bearer two"),
            ("request", "Host", "a.example", "b.example"),
            ("request", "Content-Length", "11", "5"),
            ("request", "Accept", "text/html", "application/json"),
            ("response", "Content-Type", "application/json", "text/html"),
            ("response", "ETag", '"a"', '"b"'),
        ],
        ids=[
            "request-Authorization",
            "request-Host",
            "request-Content-Length",
            "request-Accept",
            "response-Content-Type",
            "response-ETag",
        ],
    )
    def test_wording_is_right_for_a_single_value_header_and_for_a_list(
        self, location: str, name: str, first: str, second: str
    ) -> None:
        # One wording for both sides and every header. It said the header
        # was sent, which a curl command's isn't, and to join the values
        # with a comma, which is wrong for a header that takes one value.
        raw = [(name, first), (name, second)]
        if location == "request":
            cap = _cap(raw_request_headers=raw)
        else:
            cap = _cap(response=_resp(raw_headers=raw))
        finding = headers_duplicate(cap)
        assert finding is not None
        assert finding.title == "Header repeated with different values"
        assert finding.explanation == (
            f"The {location} contains multiple {name} headers with different values. "
            "A recipient may keep the first, keep the last, join them with commas or "
            "refuse the message, so two hops can read it differently."
        )
        assert finding.suggested_fix == (
            "Send each header exactly once. Join several values with commas only where "
            "the header is defined as a comma-separated list (RFC 9110, section 5.3), as "
            "Accept and Cache-Control are; a header that takes one value, such as "
            "Authorization, Host or Content-Length, can keep only one."
        )
        assert finding.severity == "warning"


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

    def test_reached_final_response_keeps_default_wording(self):
        chain = [f"https://x.com/step{i}" for i in range(6)]
        finding = redirect_too_many(_cap(response=_resp(200), redirect_chain=chain))
        assert finding is not None
        assert finding.title == "Redirect chain has 5 hops"
        assert "before reaching a final response" in finding.explanation
        assert finding.evidence is not None
        assert "stopped" not in finding.evidence

    def test_chain_stopped_on_a_redirect_says_so(self):
        # A runner that caps hops (the hosted demo's live run) returns the
        # last redirect unfollowed; the finding must not claim a final
        # response was reached.
        chain = [f"https://x.com/step{i}" for i in range(6)]
        last = _resp(302, headers={"Location": "/step6"})
        finding = redirect_too_many(_cap(response=last, redirect_chain=chain))
        assert finding is not None
        assert finding.title == "Redirect chain stopped after 5 hops"
        assert "before reaching a final response" not in finding.explanation
        assert "https://x.com/step6" in finding.explanation
        assert finding.evidence is not None
        assert finding.evidence["stopped"] is True
        assert finding.evidence["unfollowed_location"] == "https://x.com/step6"
        assert finding.evidence["redirect_count"] == 5
        assert finding.evidence["chain"] == chain


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
