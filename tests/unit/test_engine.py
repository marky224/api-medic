"""Tests for core.engine — CapturedRequest → Report assembly."""

from __future__ import annotations

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.engine import analyze
from api_medic.core.models import TimingBreakdown


def _live(**overrides) -> CapturedRequest:
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
