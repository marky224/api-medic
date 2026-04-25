"""Schema validation tests for fixture Reports.

Every fixture in tests/fixtures/reports/ must parse cleanly against the current
Pydantic schema. These tests serve three purposes:

1. They are Phase 1's acceptance criterion: "all fixtures parse cleanly."
2. They are CI smoke tests in every later phase — if a model change breaks a
   fixture, you find out immediately.
3. They double as a discovery surface: pytest's parametrization names every
   fixture file, so a glance at the test report tells you what's covered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_medic.core.models import Report

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "reports"

# Discover at module import time so pytest -k by fixture name works,
# and so missing fixtures fail loudly rather than silently passing zero tests.
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.json"))


def test_fixtures_dir_is_populated() -> None:
    """We require at least 8 fixtures per the spec."""
    assert len(FIXTURE_FILES) >= 8, (
        f"Expected at least 8 fixture files in {FIXTURES_DIR}, "
        f"found {len(FIXTURE_FILES)}: {[f.name for f in FIXTURE_FILES]}"
    )


@pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_parses_against_schema(fixture_path: Path) -> None:
    """Every fixture must parse cleanly into a Report."""
    raw = json.loads(fixture_path.read_text())
    report = Report.model_validate(raw)
    assert report.schema_version == "1.0"
    assert report.source in {"live", "har", "curl", "raw", "extension"}


@pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_findings_are_sorted(fixture_path: Path) -> None:
    """After parsing, findings must be in canonical order regardless of file order."""
    raw = json.loads(fixture_path.read_text())
    report = Report.model_validate(raw)
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    keys = [(severity_rank[f.severity], f.id) for f in report.findings]
    assert keys == sorted(keys), (
        f"Findings in {fixture_path.name} not in canonical order after parsing"
    )


@pytest.mark.parametrize("fixture_path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_round_trip_is_stable(fixture_path: Path) -> None:
    """Parse → serialize → parse → serialize produces byte-identical JSON."""
    raw = json.loads(fixture_path.read_text())
    once = Report.model_validate(raw).model_dump_json()
    twice = Report.model_validate_json(once).model_dump_json()
    assert once == twice


def test_all_distinct_ids() -> None:
    """Each fixture should have a distinct Report.id so tests don't collide."""
    ids = []
    for path in FIXTURE_FILES:
        raw = json.loads(path.read_text())
        ids.append(raw["id"])
    assert len(ids) == len(set(ids)), "Duplicate report ids across fixtures"


def test_canonical_fixture_present() -> None:
    """The 02-jwt-expired fixture is the canonical example used in the UI mockup
    and must always exist with that exact name."""
    canonical = FIXTURES_DIR / "02-jwt-expired.json"
    assert canonical.exists(), "Canonical fixture 02-jwt-expired.json is missing"
