"""Tests for core.checks.body."""

from __future__ import annotations

import gzip
from typing import Any

import pytest

from api_medic.core.captured import CapturedRequest, CapturedResponse
from api_medic.core.checks.body import (
    content_length_mismatch,
    encoding_mismatch,
    malformed_json,
)
from api_medic.core.models import TimingBreakdown


def _cap(response: CapturedResponse | None) -> CapturedRequest:
    return CapturedRequest(
        method="GET",
        url="https://api.example.com/v1/users",
        headers={},
        body=b"",
        response=response,
        timing=TimingBreakdown(),
        source="live",
    )


def _resp(
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> CapturedResponse:
    return CapturedResponse(
        status_code=status,
        status_text="",
        headers=headers or {},
        body=body,
        protocol="HTTP/1.1",
    )


class TestMalformedJson:
    def test_no_response(self):
        assert malformed_json(_cap(None)) is None

    def test_non_json_content_type_skipped(self):
        cap = _cap(_resp(headers={"Content-Type": "text/html"}, body=b"<html>"))
        assert malformed_json(cap) is None

    def test_valid_json_not_flagged(self):
        cap = _cap(
            _resp(
                headers={"Content-Type": "application/json"},
                body=b'{"ok":true}',
            )
        )
        assert malformed_json(cap) is None

    def test_invalid_json_flagged(self):
        cap = _cap(
            _resp(
                headers={"Content-Type": "application/json"},
                body=b'{"name": "alex"',  # missing closing brace
            )
        )
        finding = malformed_json(cap)
        assert finding is not None
        assert finding.id == "body.malformed_json"
        assert finding.severity == "critical"
        assert finding.evidence is not None
        assert finding.evidence["content_type"] == "application/json"
        assert "first_bytes" in finding.evidence

    def test_charset_suffix_still_matches(self):
        cap = _cap(
            _resp(
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=b"not json",
            )
        )
        finding = malformed_json(cap)
        assert finding is not None

    def test_empty_body_skipped(self):
        cap = _cap(_resp(headers={"Content-Type": "application/json"}, body=b""))
        assert malformed_json(cap) is None


# Valid JSON past Python's limits: integers over int()'s 4,300-digit limit
# (parsed without converting them), and nesting deeper than the recursion
# limit (about 10,000 levels on Python 3.12, 3,000 on Windows).
_PYTHON_LIMITS = [b"9" * 4301, b'{"n": ' + b"9" * 4301 + b"}", b"[" * 100_000 + b"]" * 100_000]
_PYTHON_LIMITS_IDS = ["4301-digit-int", "4301-digit-int-in-object", "nested-100000-deep"]


def _finding(body: bytes) -> dict[str, Any] | None:
    finding = malformed_json(_cap(_resp(headers={"Content-Type": "application/json"}, body=body)))
    return None if finding is None else finding.model_dump()


def _parse_error_finding(
    explanation_error: str, parse_error: str, first_bytes: str
) -> dict[str, Any]:
    return {
        "id": "body.malformed_json",
        "severity": "critical",
        "title": "Response body is not valid JSON",
        "explanation": (
            f"Content-Type declares JSON but the body fails to parse: {explanation_error}. "
            "Most clients will throw an exception trying to deserialize this."
        ),
        "evidence": {
            "content_type": "application/json",
            "parse_error": parse_error,
            "first_bytes": first_bytes,
        },
        "suggested_fix": (
            "Check the server output for stray HTML, BOM, or stack traces "
            "appended to the JSON. Fix the server."
        ),
    }


class TestMalformedJsonBodiesPythonCantRead:
    """The body is read as UTF-8 only, as RFC 8259 requires, with a leading
    UTF-8 BOM dropped as before. Bodies json.loads raised on with something
    other than JSONDecodeError used to make the check raise."""

    def test_invalid_json_finding_is_unchanged(self) -> None:
        assert _finding(b'{"name": "alex"') == _parse_error_finding(
            "Expecting ',' delimiter",
            "Expecting ',' delimiter: line 1 column 16 (char 15)",
            '{"name": "alex"',
        )

    @pytest.mark.parametrize(
        ("body", "parse_error"),
        [
            (b"\xef\xbb\xbf{}", None),
            (
                b"\xef\xbb\xbf{",
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            ),
            (b"\xef\xbb\xbf\xef\xbb\xbf{}", "Expecting value: line 1 column 1 (char 0)"),
        ],
        ids=["bom-valid", "bom-invalid", "two-boms"],
    )
    def test_utf_8_bom_is_dropped_as_before(self, body: bytes, parse_error: str | None) -> None:
        finding = _finding(body)
        if parse_error is None:
            assert finding is None
        else:
            assert finding is not None
            assert finding["evidence"]["parse_error"] == parse_error

    @pytest.mark.parametrize(
        ("body", "reason", "parse_error", "first_bytes"),
        [
            (
                b'{"name": "caf\xe9"}',
                "invalid continuation byte",
                "'utf-8' codec can't decode byte 0xe9 in position 13: invalid continuation byte",
                '{"name": "caf\ufffd"}',
            ),
            (
                b"\xff",
                "invalid start byte",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
                "\ufffd",
            ),
            # UTF-16 and UTF-32 JSON, which json.loads detects and used to accept.
            (
                b"\xff\xfe{\x00}\x00",
                "invalid start byte",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
                "\ufffd\ufffd{\x00}\x00",
            ),
            (
                b"\xfe\xff\x00{\x00}",
                "invalid start byte",
                "'utf-8' codec can't decode byte 0xfe in position 0: invalid start byte",
                "\ufffd\ufffd\x00{\x00}",
            ),
            (
                b"\xff\xfe\x00\x00{\x00\x00\x00}\x00\x00\x00",
                "invalid start byte",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
                "\ufffd\ufffd\x00\x00{\x00\x00\x00}\x00\x00\x00",
            ),
            # A UTF-8-encoded surrogate, which json.loads used to let through.
            (
                b'"\xed\xa0\x80"',
                "invalid continuation byte",
                "'utf-8' codec can't decode byte 0xed in position 1: invalid continuation byte",
                '"\ufffd\ufffd\ufffd"',
            ),
            (
                b"\xef\xbb\xbf{\xff}",
                "invalid start byte",
                "'utf-8' codec can't decode byte 0xff in position 1: invalid start byte",
                "\ufeff{\ufffd}",
            ),
        ],
        ids=[
            "latin-1",
            "lone-ff",
            "utf-16-le-bom",
            "utf-16-be-bom",
            "utf-32-le-bom",
            "encoded-surrogate",
            "bom-then-ff",
        ],
    )
    def test_body_that_is_not_utf_8_is_flagged(
        self, body: bytes, reason: str, parse_error: str, first_bytes: str
    ) -> None:
        assert _finding(body) == {
            "id": "body.malformed_json",
            "severity": "critical",
            "title": "Response body is not valid JSON",
            "explanation": (
                "Content-Type declares JSON but the body can't be decoded as UTF-8 "
                f"text: {reason}. JSON must be UTF-8 (RFC 8259). Most clients will "
                "throw an exception trying to deserialize this."
            ),
            "evidence": {
                "content_type": "application/json",
                "parse_error": parse_error,
                "first_bytes": first_bytes,
            },
            "suggested_fix": (
                "Send the body as UTF-8, or fix the Content-Type if the body isn't JSON."
            ),
        }

    @pytest.mark.parametrize(
        ("body", "explanation_error", "parse_error"),
        [
            # NUL bytes made json.loads read these as UTF-16; they are UTF-8.
            (
                b"{\x00}\x00",
                "Expecting property name enclosed in double quotes",
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            ),
            (b"\x00{\x00}", "Expecting value", "Expecting value: line 1 column 1 (char 0)"),
            (
                b"{\x00}\x00 ",
                "Expecting property name enclosed in double quotes",
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            ),
            # Malformed JSON holding an integer over int()'s limit used to be
            # skipped: int() raised before the parser reached the error.
            (b"9" * 4301 + b"x", "Extra data", "Extra data: line 1 column 4302 (char 4301)"),
            (
                b'{"a": ' + b"9" * 4301 + b', "b": }',
                "Expecting value",
                "Expecting value: line 1 column 4315 (char 4314)",
            ),
        ],
        ids=[
            "utf-16-le-nul",
            "utf-16-be-nul",
            "odd-length-nul",
            "long-int-then-junk",
            "long-int-then-missing-value",
        ],
    )
    def test_body_that_fails_to_parse_as_utf_8_is_flagged(
        self, body: bytes, explanation_error: str, parse_error: str
    ) -> None:
        first_bytes = body[:120].decode("utf-8", errors="replace")
        assert _finding(body) == _parse_error_finding(explanation_error, parse_error, first_bytes)

    @pytest.mark.parametrize("body", _PYTHON_LIMITS, ids=_PYTHON_LIMITS_IDS)
    def test_valid_json_past_python_limits_is_not_flagged(self, body: bytes) -> None:
        assert _finding(body) is None

    def test_deep_nesting_is_skipped_even_when_malformed(self) -> None:
        # The parser hits the recursion limit before it reaches the error.
        assert _finding(b"[" * 100_000) is None


class TestContentLengthMismatch:
    def test_no_response(self):
        assert content_length_mismatch(_cap(None)) is None

    def test_no_content_length_header_skipped(self):
        cap = _cap(_resp(body=b"hello"))
        assert content_length_mismatch(cap) is None

    def test_match_not_flagged(self):
        cap = _cap(_resp(headers={"Content-Length": "5"}, body=b"hello"))
        assert content_length_mismatch(cap) is None

    def test_mismatch_flagged(self):
        cap = _cap(_resp(headers={"Content-Length": "10"}, body=b"hello"))
        finding = content_length_mismatch(cap)
        assert finding is not None
        assert finding.id == "body.content_length_mismatch"
        assert finding.severity == "warning"
        assert finding.evidence == {
            "declared_bytes": 10,
            "actual_bytes": 5,
            "diff": -5,
        }

    def test_decoded_body_skipped(self):
        # If the runner decoded gzip, length naturally differs from header.
        cap = _cap(
            _resp(
                headers={"Content-Length": "100", "Content-Encoding": "gzip"},
                body=b"plain text after decode",
            )
        )
        assert content_length_mismatch(cap) is None

    def test_chunked_skipped(self):
        cap = _cap(
            _resp(
                headers={"Content-Length": "100", "Transfer-Encoding": "chunked"},
                body=b"hi",
            )
        )
        assert content_length_mismatch(cap) is None

    def test_garbage_content_length_skipped(self):
        cap = _cap(_resp(headers={"Content-Length": "not-a-number"}, body=b"hi"))
        assert content_length_mismatch(cap) is None


class TestEncodingMismatch:
    def test_no_response(self):
        assert encoding_mismatch(_cap(None)) is None

    def test_no_content_encoding_skipped(self):
        cap = _cap(_resp(body=b"hello"))
        assert encoding_mismatch(cap) is None

    def test_gzip_with_magic_bytes_not_flagged(self):
        body = gzip.compress(b"hello world")
        cap = _cap(_resp(headers={"Content-Encoding": "gzip"}, body=body))
        assert encoding_mismatch(cap) is None

    def test_gzip_without_magic_bytes_flagged(self):
        cap = _cap(
            _resp(
                headers={"Content-Encoding": "gzip"},
                body=b'{"hi":true}',  # plain JSON, claimed gzip
            )
        )
        finding = encoding_mismatch(cap)
        assert finding is not None
        assert finding.id == "body.encoding_mismatch"
        assert finding.evidence is not None
        assert finding.evidence["declared_encoding"] == "gzip"
        assert "first_bytes_hex" in finding.evidence

    def test_brotli_skipped(self):
        # Brotli has no reliable magic prefix; we'd false-positive.
        cap = _cap(_resp(headers={"Content-Encoding": "br"}, body=b"plain text"))
        assert encoding_mismatch(cap) is None

    def test_deflate_skipped(self):
        cap = _cap(_resp(headers={"Content-Encoding": "deflate"}, body=b"plain text"))
        assert encoding_mismatch(cap) is None

    def test_empty_body_skipped(self):
        cap = _cap(_resp(headers={"Content-Encoding": "gzip"}, body=b""))
        assert encoding_mismatch(cap) is None
