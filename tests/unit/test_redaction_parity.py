"""Redaction parity: masking credentials in the browser extension keeps findings.

tests/fixtures/redaction/ holds HAR entries with credentials in them. The
extension's extension/src/lib/redact.test.ts writes the masked copy of each as
<name>.redacted.json (a vitest file snapshot, so CI fails when one goes stale).
These tests run the real parser and checks on both copies: masking must never
change which findings fire, and a masked JWT keeps only its exp, nbf and sub
claims.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from api_medic.core.checks.auth import _bearer_token, _decode_jwt_payload
from api_medic.core.engine import analyze
from api_medic.core.models import Report
from api_medic.core.parser import parse_har

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "redaction"
HEADER_LIST = FIXTURES_DIR / "check-read-headers.json"
CHECKS_DIR = Path(__file__).parents[2] / "src" / "api_medic" / "core" / "checks"

SCENARIOS = sorted(
    p
    for p in FIXTURES_DIR.glob("*.json")
    if p != HEADER_LIST and not p.name.endswith(".redacted.json")
)

# Evidence that legitimately changes when the input is masked: the
# Authorization value itself, and durations measured against the clock.
EVIDENCE_EXPECTED_TO_DIFFER = {"header_value_repr", "expired_for_seconds", "valid_in_seconds"}

KEPT_CLAIMS = {"exp", "nbf", "sub"}


def _load(path: Path) -> Any:
    # Explicit UTF-8: CI also runs on Windows.
    return json.loads(path.read_text(encoding="utf-8"))


def _redacted(path: Path) -> Path:
    return path.with_name(f"{path.stem}.redacted.json")


def _analyze(entry: dict[str, Any]) -> Report:
    har = {
        "log": {"version": "1.2", "creator": {"name": "test", "version": "0"}, "entries": [entry]}
    }
    return analyze(parse_har(har))


def _jwt_claims(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Claims of the entry's Bearer JWT, decoded the way the auth checks decode them."""
    for header in entry["request"].get("headers", []):
        if header["name"].lower() == "authorization":
            token = _bearer_token(header["value"])
            return _decode_jwt_payload(token) if token is not None else None
    return None


JWT_SCENARIOS = [p for p in SCENARIOS if _jwt_claims(_load(p)["entry"]) is not None]


def test_fixtures_dir_is_populated() -> None:
    assert len(SCENARIOS) >= 9
    assert len(JWT_SCENARIOS) >= 4


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_masking_keeps_findings(path: Path) -> None:
    fixture = _load(path)
    before = _analyze(fixture["entry"])
    after = _analyze(_load(_redacted(path)))

    assert sorted(f.id for f in before.findings) == sorted(fixture["expect_findings"])
    assert [f.id for f in after.findings] == [f.id for f in before.findings]
    for b, a in zip(before.findings, after.findings, strict=True):
        b_evidence = {
            k: v for k, v in (b.evidence or {}).items() if k not in EVIDENCE_EXPECTED_TO_DIFFER
        }
        a_evidence = {
            k: v for k, v in (a.evidence or {}).items() if k not in EVIDENCE_EXPECTED_TO_DIFFER
        }
        assert a_evidence == b_evidence, b.id


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_masked_entry_holds_no_secret(path: Path) -> None:
    text = _redacted(path).read_text(encoding="utf-8")
    for secret in _load(path)["secrets"]:
        assert secret not in text


@pytest.mark.parametrize("path", JWT_SCENARIOS, ids=lambda p: p.stem)
def test_masked_jwt_keeps_only_expiry_and_subject(path: Path) -> None:
    original = _jwt_claims(_load(path)["entry"])
    masked = _jwt_claims(_load(_redacted(path)))
    assert original is not None
    assert masked is not None, "masked token no longer decodes as a JWT"

    assert set(masked) == KEPT_CLAIMS & set(original)
    for claim, value in masked.items():
        assert value == original[claim]
        assert type(value) is type(original[claim])

    dropped = [v for k, v in original.items() if k not in KEPT_CLAIMS]
    assert dropped, "each JWT fixture carries a claim that masking must drop"
    masked_text = json.dumps(masked) + _redacted(path).read_text(encoding="utf-8")
    for value in dropped:
        if isinstance(value, str):
            assert value not in masked_text


def _headers_read_by_checks() -> set[str]:
    names: set[str] = set()
    for path in CHECKS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"_find", "_find_header"}
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
    return names


def test_check_read_headers_list_is_current() -> None:
    """A check that starts reading a new header fails here until the header is
    added to check-read-headers.json, where the extension's tests prove the
    masking leaves it alone."""
    assert _headers_read_by_checks() == set(_load(HEADER_LIST))
