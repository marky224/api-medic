"""Tests for core.checks.network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api_medic.core.captured import CapturedDns, CapturedRequest, CapturedTls
from api_medic.core.checks.network import (
    dns_no_records,
    dns_slow,
    tls_cn_mismatch,
    tls_expired,
    tls_expiring_soon,
    tls_weak_protocol,
)
from api_medic.core.models import TimingBreakdown


def _cap(
    url: str = "https://api.example.com/v1/users",
    dns: CapturedDns | None = None,
    tls: CapturedTls | None = None,
    dns_ms: float | None = None,
) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url=url,
        headers={},
        body=b"",
        response=None,
        timing=TimingBreakdown(dns_ms=dns_ms),
        dns=dns,
        tls=tls,
        source="live",
    )


def _tls(
    not_after_offset_days: float = 365,
    not_before_offset_days: float = -30,
    cn: str | None = "api.example.com",
    sans: list[str] | None = None,
    version: str = "TLSv1.3",
) -> CapturedTls:
    now = datetime.now(timezone.utc)
    return CapturedTls(
        not_before=now + timedelta(days=not_before_offset_days),
        not_after=now + timedelta(days=not_after_offset_days),
        subject_common_name=cn,
        subject_alt_names=sans or ["api.example.com"],
        issuer_common_name="Test CA",
        negotiated_protocol_version=version,
    )


class TestDnsNoRecords:
    def test_no_dns_data_skipped(self):
        assert dns_no_records(_cap()) is None

    def test_records_present_not_flagged(self):
        cap = _cap(dns=CapturedDns(records=["1.2.3.4"]))
        assert dns_no_records(cap) is None

    def test_empty_records_flagged(self):
        cap = _cap(dns=CapturedDns(records=[]))
        finding = dns_no_records(cap)
        assert finding is not None
        assert finding.id == "network.dns.no_records"
        assert finding.severity == "critical"
        assert finding.evidence == {"host": "api.example.com"}


class TestDnsSlow:
    def test_no_dns_ms_skipped(self):
        cap = _cap(dns=CapturedDns(records=["1.2.3.4"]))
        assert dns_slow(cap) is None

    def test_below_threshold_not_flagged(self):
        cap = _cap(dns=CapturedDns(records=["1.2.3.4"]), dns_ms=200.0)
        assert dns_slow(cap) is None

    def test_above_threshold_flagged(self):
        cap = _cap(dns=CapturedDns(records=["1.2.3.4"]), dns_ms=750.0)
        finding = dns_slow(cap)
        assert finding is not None
        assert finding.id == "network.dns.slow"
        assert finding.severity == "warning"
        assert finding.evidence is not None
        assert finding.evidence["dns_ms"] == 750.0

    def test_no_records_skipped_in_favour_of_no_records_check(self):
        # Don't double-flag a fully-failed lookup as "slow."
        cap = _cap(dns=CapturedDns(records=[]), dns_ms=2000.0)
        assert dns_slow(cap) is None


class TestTlsExpired:
    def test_no_tls_skipped(self):
        assert tls_expired(_cap()) is None

    def test_future_not_after_not_flagged(self):
        cap = _cap(tls=_tls(not_after_offset_days=30))
        assert tls_expired(cap) is None

    def test_past_not_after_flagged(self):
        cap = _cap(tls=_tls(not_after_offset_days=-2))
        finding = tls_expired(cap)
        assert finding is not None
        assert finding.id == "network.tls.expired"
        assert finding.severity == "critical"
        assert finding.evidence is not None
        assert "not_after" in finding.evidence
        assert finding.evidence["expired_for_seconds"] >= 86400


class TestTlsExpiringSoon:
    def test_no_tls_skipped(self):
        assert tls_expiring_soon(_cap()) is None

    def test_far_future_not_flagged(self):
        cap = _cap(tls=_tls(not_after_offset_days=90))
        assert tls_expiring_soon(cap) is None

    def test_within_threshold_flagged(self):
        cap = _cap(tls=_tls(not_after_offset_days=5))
        finding = tls_expiring_soon(cap)
        assert finding is not None
        assert finding.id == "network.tls.expiring_soon"
        assert finding.severity == "warning"

    def test_already_expired_skipped(self):
        cap = _cap(tls=_tls(not_after_offset_days=-1))
        assert tls_expiring_soon(cap) is None


class TestTlsWeakProtocol:
    def test_no_tls_skipped(self):
        assert tls_weak_protocol(_cap()) is None

    def test_tlsv13_not_flagged(self):
        cap = _cap(tls=_tls(version="TLSv1.3"))
        assert tls_weak_protocol(cap) is None

    def test_tlsv12_not_flagged(self):
        cap = _cap(tls=_tls(version="TLSv1.2"))
        assert tls_weak_protocol(cap) is None

    def test_tlsv11_flagged(self):
        cap = _cap(tls=_tls(version="TLSv1.1"))
        finding = tls_weak_protocol(cap)
        assert finding is not None
        assert finding.id == "network.tls.weak_protocol"
        assert finding.severity == "critical"
        assert finding.evidence is not None
        assert finding.evidence["negotiated_version"] == "TLSv1.1"

    def test_tlsv10_flagged(self):
        cap = _cap(tls=_tls(version="TLSv1.0"))
        finding = tls_weak_protocol(cap)
        assert finding is not None

    def test_unknown_version_skipped(self):
        cap = _cap(tls=_tls(version=""))
        assert tls_weak_protocol(cap) is None


class TestTlsCnMismatch:
    def test_no_tls_skipped(self):
        assert tls_cn_mismatch(_cap()) is None

    def test_exact_cn_match_not_flagged(self):
        cap = _cap(
            url="https://api.example.com/x",
            tls=_tls(cn="api.example.com", sans=["api.example.com"]),
        )
        assert tls_cn_mismatch(cap) is None

    def test_san_match_not_flagged(self):
        cap = _cap(
            url="https://api.example.com/x",
            tls=_tls(cn="other.example.com", sans=["api.example.com", "www.example.com"]),
        )
        assert tls_cn_mismatch(cap) is None

    def test_wildcard_san_matches_one_label(self):
        cap = _cap(
            url="https://api.example.com/x",
            tls=_tls(cn=None, sans=["*.example.com"]),
        )
        assert tls_cn_mismatch(cap) is None

    def test_wildcard_san_does_not_match_root_domain(self):
        cap = _cap(
            url="https://example.com/x",
            tls=_tls(cn=None, sans=["*.example.com"]),
        )
        finding = tls_cn_mismatch(cap)
        assert finding is not None

    def test_wildcard_san_does_not_match_two_labels(self):
        cap = _cap(
            url="https://foo.bar.example.com/x",
            tls=_tls(cn=None, sans=["*.example.com"]),
        )
        finding = tls_cn_mismatch(cap)
        assert finding is not None

    def test_no_match_flagged(self):
        cap = _cap(
            url="https://api.example.com/x",
            tls=_tls(cn="api.example.org", sans=["api.example.org"]),
        )
        finding = tls_cn_mismatch(cap)
        assert finding is not None
        assert finding.id == "network.tls.cn_mismatch"
        assert finding.severity == "critical"
        ev = finding.evidence
        assert ev is not None
        assert ev["request_host"] == "api.example.com"
        assert ev["cert_common_name"] == "api.example.org"

    def test_case_insensitive_match(self):
        cap = _cap(
            url="https://API.Example.COM/x",
            tls=_tls(cn="api.example.com", sans=["api.example.com"]),
        )
        assert tls_cn_mismatch(cap) is None
