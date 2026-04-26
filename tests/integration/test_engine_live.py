"""Live integration tests against httpbin.org.

All tests are marked @pytest.mark.integration. Default `pytest` runs skip
them; CI runs them in a separate, flake-tolerant job (see ci.yml).

httpbin.org is the canonical public test endpoint per the architecture
doc's open-questions list. If it proves consistently flaky we can fall
back to postman-echo.com or stand up a minimal test server in deploy/.
"""

from __future__ import annotations

import pytest

from api_medic.core.engine import analyze
from api_medic.core.runner import run

pytestmark = pytest.mark.integration

HTTPBIN = "https://httpbin.org"


def test_basic_get_returns_200():
    cap = run("GET", f"{HTTPBIN}/status/200")
    assert cap.response is not None
    assert cap.response.status_code == 200


def test_401_status_passes_through():
    cap = run("GET", f"{HTTPBIN}/status/401")
    assert cap.response is not None
    assert cap.response.status_code == 401


def test_429_with_no_retry_after_fires_rate_limit_hit():
    cap = run("GET", f"{HTTPBIN}/status/429")
    report = analyze(cap)
    ids = [f.id for f in report.findings]
    assert "rate_limit.hit" in ids


def test_redirect_chain_populated():
    cap = run("GET", f"{HTTPBIN}/redirect/2")
    # /redirect/2 → /relative-redirect/1 → /get
    assert cap.redirect_chain is not None
    assert len(cap.redirect_chain) >= 3


def test_dns_probe_resolves_real_host():
    cap = run("GET", f"{HTTPBIN}/status/200")
    assert cap.dns is not None
    assert len(cap.dns.records) >= 1
    assert cap.timing.dns_ms is not None
    assert cap.timing.dns_ms >= 0


def test_tls_probe_captures_cert_for_https():
    cap = run("GET", f"{HTTPBIN}/status/200")
    assert cap.tls is not None
    assert cap.tls.negotiated_protocol_version.startswith("TLSv1")
    # httpbin's cert should match the requested host (no cn_mismatch finding).
    report = analyze(cap)
    ids = [f.id for f in report.findings]
    assert "network.tls.cn_mismatch" not in ids
