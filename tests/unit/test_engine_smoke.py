"""Engine smoke test against the Phase-1 fixtures.

This is the 3a-Step-5 milestone: prove the engine, fed a CapturedRequest
equivalent to a hand-crafted fixture, produces the diagnostic the fixture
promised. We don't assert exact evidence values (those depend on current
time relative to the JWT's exp) — just that the right check fires.
"""

from __future__ import annotations

import json
from pathlib import Path

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.engine import analyze
from api_medic.core.models import TimingBreakdown

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "reports"


def _captured_from_fixture(name: str) -> CapturedRequest:
    """Reconstruct a CapturedRequest from a fixture Report.

    Body bytes come from `body_preview` (acceptable approximation for the
    text-bodied fixtures we ship in v1).
    """
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    req = data["request"]
    resp = data.get("response")

    captured_response: CapturedResponse | None = None
    if resp:
        captured_response = CapturedResponse(
            status_code=resp["status_code"],
            status_text=resp["status_text"],
            headers=resp.get("headers", {}),
            body=(resp.get("body_preview") or "").encode("utf-8"),
            protocol=resp.get("protocol", "HTTP/1.1"),
        )

    return CapturedRequest(
        method=req["method"],
        url=req["url"],
        headers=req.get("headers", {}),
        body=(req.get("body_preview") or "").encode("utf-8"),
        response=captured_response,
        timing=TimingBreakdown(**data.get("timing", {})),
        source=data.get("source", "live"),
    )


class TestAnalyzeAgainstCanonicalFixture:
    def test_jwt_expired_fixture_produces_jwt_expired_finding(self):
        cap = _captured_from_fixture("02-jwt-expired.json")
        report = analyze(cap)

        ids = [f.id for f in report.findings]
        assert "auth.jwt.expired" in ids

        finding = next(f for f in report.findings if f.id == "auth.jwt.expired")
        assert finding.severity == "critical"
        assert "expired" in finding.title.lower()
        assert finding.evidence is not None
        assert "exp" in finding.evidence
        assert "expired_for_seconds" in finding.evidence

    def test_jwt_expired_fixture_report_has_expected_request_response(self):
        # Engine should faithfully echo the request/response into the Report.
        cap = _captured_from_fixture("02-jwt-expired.json")
        report = analyze(cap)
        assert report.request.method == "POST"
        assert report.request.url.endswith("/v1/users")
        assert report.response is not None
        assert report.response.status_code == 401

    def test_healthy_fixture_does_not_produce_jwt_finding(self):
        cap = _captured_from_fixture("01-healthy.json")
        report = analyze(cap)

        ids = [f.id for f in report.findings]
        assert "auth.jwt.expired" not in ids
