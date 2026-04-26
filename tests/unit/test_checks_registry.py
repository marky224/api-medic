"""Tests for the check registry plumbing (registration, dispatch, sorting)."""

from __future__ import annotations

from api_medic.core.captured import CapturedRequest
from api_medic.core.checks import CheckRegistry
from api_medic.core.models import Finding, TimingBreakdown


def _cap(**overrides) -> CapturedRequest:
    base = {
        "method": "GET",
        "url": "https://example.com/",
        "headers": {},
        "body": b"",
        "response": None,
        "timing": TimingBreakdown(),
        "source": "live",
    }
    return CapturedRequest(**(base | overrides))


class TestCheckRegistry:
    def test_empty_registry_yields_no_findings(self):
        reg = CheckRegistry()
        assert reg.run(_cap()) == []

    def test_register_then_run_returns_finding(self):
        reg = CheckRegistry()

        @reg.register
        def returns_one(_: CapturedRequest) -> Finding:
            return Finding(
                id="test.example.one",
                severity="info",
                title="example",
                explanation="hi",
            )

        findings = reg.run(_cap())
        assert len(findings) == 1
        assert findings[0].id == "test.example.one"

    def test_check_returning_none_is_skipped(self):
        reg = CheckRegistry()

        @reg.register
        def silent(_: CapturedRequest) -> None:
            return None

        assert reg.run(_cap()) == []

    def test_check_returning_a_list_is_flattened(self):
        reg = CheckRegistry()

        @reg.register
        def returns_two(_: CapturedRequest) -> list[Finding]:
            return [
                Finding(id="test.a.one", severity="info", title="a", explanation="x"),
                Finding(id="test.b.one", severity="warning", title="b", explanation="x"),
            ]

        findings = reg.run(_cap())
        assert len(findings) == 2
        assert {f.id for f in findings} == {"test.a.one", "test.b.one"}

    def test_all_returns_a_copy(self):
        reg = CheckRegistry()

        @reg.register
        def x(_: CapturedRequest) -> None:
            return None

        snapshot = reg.all()
        snapshot.clear()
        assert len(reg.all()) == 1  # didn't mutate the internal list
