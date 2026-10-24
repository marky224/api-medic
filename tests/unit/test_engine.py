"""Tests for core.engine — CapturedRequest → Report assembly."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from pydantic_core import PydanticSerializationError

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.engine import analyze
from api_medic.core.models import TimingBreakdown, is_unencodable_text_error
from api_medic.core.parser import parse_curl


def _live(**overrides: Any) -> CapturedRequest:
    base = {
        "method": "GET",
        "url": "https://api.example.com/v1/health",
        "headers": {"Accept": "application/json"},
        "body": b"",
        "response": CapturedResponse(
            status_code=200,
            status_text="OK",
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
            protocol="HTTP/2",
        ),
        "timing": TimingBreakdown(total_ms=120.0),
        "source": "live",
    }
    return CapturedRequest(**(base | overrides))


class TestAnalyze:
    def test_produces_report_matching_captured_input(self):
        cap = _live()
        report = analyze(cap)
        assert report.source == "live"
        assert report.request.method == "GET"
        assert report.request.url == cap.url
        assert report.request.headers == {"Accept": "application/json"}
        assert report.request.body_size_bytes == 0
        assert report.request.body_preview is None
        assert report.response is not None
        assert report.response.status_code == 200
        assert report.response.body_size_bytes == len(b'{"ok":true}')
        assert report.response.body_preview == '{"ok":true}'
        assert report.timing.total_ms == 120.0

    def test_omits_response_when_runner_yielded_none(self):
        cap = _live(response=None)
        report = analyze(cap)
        assert report.response is None

    def test_truncates_long_body_to_500_chars(self):
        long_body = b"x" * 1000
        cap = _live(body=long_body)
        report = analyze(cap)
        assert report.request.body_size_bytes == 1000
        assert report.request.body_preview is not None
        assert len(report.request.body_preview) == 500

    def test_binary_body_yields_no_preview(self):
        cap = _live(body=b"\xff\xfe\x00")
        report = analyze(cap)
        assert report.request.body_preview is None
        assert report.request.body_size_bytes == 3

    def test_uuid_and_timestamp_auto_populated(self):
        report = analyze(_live())
        assert report.id  # uuid4 default
        assert report.timestamp is not None
        assert report.schema_version == "1.0"

    def test_findings_default_to_empty_when_no_check_fires(self):
        # No checks fire on a healthy 200/JSON response yet (jwt.expired needs a JWT).
        report = analyze(_live())
        assert report.findings == []


def _jwt(payload: bytes) -> str:
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{b64}.sig"


# Payload {"sub":"\ud800","exp":0} as written: JSON's escape for an
# unpaired surrogate, which json.loads decodes as one.
_SURROGATE_SUB_JWT = _jwt(b'{"sub":"\\ud800","exp":0}')


class TestUnpairedSurrogate:
    """Input can carry an unpaired surrogate into the Report: a decoded JWT's
    `sub` copied into evidence, or any header value."""

    @pytest.mark.parametrize(
        "header",
        [f"Authorization: Bearer {_SURROGATE_SUB_JWT}", "X-Note: a\ud800b"],
        ids=["jwt-sub", "header-value"],
    )
    def test_report_cannot_be_encoded_as_json(self, header: str) -> None:
        report = analyze(parse_curl(f"curl https://api.example.com/ -H '{header}'"))
        with pytest.raises(PydanticSerializationError) as e:
            report.model_dump_json()
        assert is_unencodable_text_error(e.value)


_RATE_LIMIT_HEADERS = {
    "Retry-After": "60",
    "X-RateLimit-Limit": "1000",
    "X-RateLimit-Remaining": "5",
    "X-RateLimit-Reset": "1761399420",
}


class TestUnparseableRateLimitValues:
    """A rate-limit header no int comes from gives the findings of a
    response without it. These used to make analyze() raise ValueError."""

    @staticmethod
    def _findings(status: int, headers: dict[str, str]) -> list[Any]:
        response = CapturedResponse(
            status_code=status, status_text="", headers=headers, body=b"", protocol="HTTP/2"
        )
        return analyze(_live(response=response)).findings

    @pytest.mark.parametrize(
        "value", ["9" * 4301, "\u00b2"], ids=["4301-digits", "superscript-two"]
    )
    @pytest.mark.parametrize("name", list(_RATE_LIMIT_HEADERS))
    @pytest.mark.parametrize("status", [429, 200])
    def test_findings_match_a_response_without_it(self, status: int, name: str, value: str) -> None:
        without = {k: v for k, v in _RATE_LIMIT_HEADERS.items() if k != name}
        assert self._findings(status, without | {name: value}) == self._findings(status, without)


class TestMalformedJsonBodiesPythonCantRead:
    """JSON-typed bodies json.loads raises on with something other than
    JSONDecodeError. These used to make analyze() raise."""

    @staticmethod
    def _findings(body: bytes) -> list[Any]:
        response = CapturedResponse(
            status_code=200,
            status_text="OK",
            headers={"Content-Type": "application/json"},
            body=body,
            protocol="HTTP/2",
        )
        return analyze(_live(response=response)).findings

    def test_body_that_is_not_utf_8_gets_malformed_json(self) -> None:
        assert [f.id for f in self._findings(b'{"name": "caf\xe9"}')] == ["body.malformed_json"]

    @pytest.mark.parametrize(
        "body",
        [b"9" * 4301, b"[" * 100_000 + b"]" * 100_000],
        ids=["4301-digit-int", "nested-100000-deep"],
    )
    def test_valid_json_past_python_limits_gives_the_findings_of_valid_json(
        self, body: bytes
    ) -> None:
        assert self._findings(body) == self._findings(b"[]")
