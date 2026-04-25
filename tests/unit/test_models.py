"""Tests for the core Pydantic models.

These tests lock in the contract that every surface (CLI, web, Lambda, extension)
depends on. Breaking changes here are schema-version-bumping changes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api_medic.core.models import (
    Finding,
    Report,
    RequestSummary,
    ResponseSummary,
    TimingBreakdown,
)


def _minimal_request() -> RequestSummary:
    return RequestSummary(method="GET", url="https://example.com", body_size_bytes=0)


def _minimal_response() -> ResponseSummary:
    return ResponseSummary(
        status_code=200,
        status_text="OK",
        body_size_bytes=0,
        protocol="HTTP/2",
    )


def _minimal_report(**overrides: object) -> Report:
    base: dict[str, object] = {
        "source": "live",
        "request": _minimal_request(),
        "response": _minimal_response(),
        "timing": TimingBreakdown(),
        "findings": [],
    }
    base.update(overrides)
    return Report.model_validate(base)


class TestFinding:
    def test_minimal_valid(self) -> None:
        f = Finding(
            id="auth.jwt.expired",
            severity="critical",
            title="Expired",
            explanation="The token expired.",
        )
        assert f.evidence is None
        assert f.suggested_fix is None

    @pytest.mark.parametrize(
        "bad_id",
        [
            "no_dot",
            "Uppercase.id",
            ".leading.dot",
            "trailing.dot.",
            "double..dot",
            "1.starts_with_digit",
            "has-hyphen.in_segment",
        ],
    )
    def test_id_pattern_rejects_malformed(self, bad_id: str) -> None:
        with pytest.raises(ValidationError):
            Finding(id=bad_id, severity="info", title="t", explanation="e")

    @pytest.mark.parametrize(
        "good_id",
        [
            "auth.jwt.expired",
            "network.dns.no_records",
            "http.cors.misconfigured",
            "rate_limit.hit",
            "info.protocol.http2",
            "a.b",
            "with_underscore.in_segment.too",
        ],
    )
    def test_id_pattern_accepts_namespaced(self, good_id: str) -> None:
        f = Finding(id=good_id, severity="info", title="t", explanation="e")
        assert f.id == good_id

    def test_severity_must_be_one_of_three(self) -> None:
        with pytest.raises(ValidationError):
            Finding(id="a.b", severity="urgent", title="t", explanation="e")  # type: ignore[arg-type,unused-ignore]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Finding(
                id="a.b",
                severity="info",
                title="t",
                explanation="e",
                unexpected_field="nope",  # type: ignore[call-arg]
            )

    def test_title_and_explanation_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            Finding(id="a.b", severity="info", title="", explanation="ok")
        with pytest.raises(ValidationError):
            Finding(id="a.b", severity="info", title="ok", explanation="")


class TestTimingBreakdown:
    def test_all_optional(self) -> None:
        t = TimingBreakdown()
        assert t.dns_ms is None
        assert t.total_ms is None

    def test_negative_values_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimingBreakdown(dns_ms=-1.0)


class TestResponseSummary:
    @pytest.mark.parametrize("bad_status", [99, 600, 0, -1])
    def test_status_code_range(self, bad_status: int) -> None:
        with pytest.raises(ValidationError):
            ResponseSummary(
                status_code=bad_status,
                status_text="x",
                body_size_bytes=0,
                protocol="HTTP/2",
            )


class TestReportFindingSort:
    """Findings must be sorted critical → warning → info, then by id alphabetically."""

    def test_critical_before_warning_before_info(self) -> None:
        report = _minimal_report(
            findings=[
                Finding(id="z.info.first", severity="info", title="t", explanation="e"),
                Finding(id="a.warning.first", severity="warning", title="t", explanation="e"),
                Finding(id="m.critical.first", severity="critical", title="t", explanation="e"),
            ]
        )
        assert [f.severity for f in report.findings] == ["critical", "warning", "info"]

    def test_alphabetical_within_severity(self) -> None:
        report = _minimal_report(
            findings=[
                Finding(id="auth.zeta", severity="critical", title="t", explanation="e"),
                Finding(id="auth.alpha", severity="critical", title="t", explanation="e"),
                Finding(id="auth.mu", severity="critical", title="t", explanation="e"),
            ]
        )
        assert [f.id for f in report.findings] == ["auth.alpha", "auth.mu", "auth.zeta"]

    def test_sort_is_deterministic_across_input_order(self) -> None:
        a = Finding(id="a.b", severity="critical", title="t", explanation="e")
        b = Finding(id="c.d", severity="warning", title="t", explanation="e")
        c = Finding(id="e.f", severity="info", title="t", explanation="e")

        report_one = _minimal_report(findings=[a, b, c])
        report_two = _minimal_report(findings=[c, b, a])
        report_three = _minimal_report(findings=[b, a, c])

        ids_one = [f.id for f in report_one.findings]
        ids_two = [f.id for f in report_two.findings]
        ids_three = [f.id for f in report_three.findings]
        assert ids_one == ids_two == ids_three


class TestReportRoundTrip:
    def test_serialize_deserialize_is_lossless(self) -> None:
        original = _minimal_report(
            findings=[
                Finding(
                    id="auth.jwt.expired",
                    severity="critical",
                    title="Expired",
                    explanation="...",
                    evidence={"exp": "2026-01-01T00:00:00Z"},
                    suggested_fix="Refresh the token.",
                )
            ]
        )
        as_json = original.model_dump_json()
        roundtripped = Report.model_validate_json(as_json)
        assert roundtripped == original

    def test_default_factories_populate_id_and_timestamp(self) -> None:
        report = _minimal_report()
        assert len(report.id) == 36  # uuid4 string length
        assert report.timestamp is not None
        assert report.schema_version == "1.0"

    def test_response_can_be_null_for_failed_requests(self) -> None:
        report = _minimal_report(response=None)
        assert report.response is None
