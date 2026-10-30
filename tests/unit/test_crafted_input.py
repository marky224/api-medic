"""Crafted input through every entry point.

One table of crafted inputs. Each row goes through every entry point
that takes its kind:

  har   the hosted demo's /api/analyze, with the HAR in the body and as a
        JSON string; api-medic serve's /api/analyze; `api-medic from-har`
  curl  both /api/analyze routes; `api-medic from-curl --no-execute`
  run   both /api/run routes, `api-medic run` and `api-medic from-curl`,
        the runner answering with the row's response (no DNS lookup, TLS
        probe or network)
  body  a request body only the two servers take

Each must end in a report (200, exit 0) or a 400 (422 where FastAPI's
validation answers) or exit 2 carrying a message: never a 500 or a
traceback. A row names what every entry point gives, a report or the
message, and what the ones that differ give. The CLI runs in every
output format: -o json gives the row's ending, and so do the others,
except that one that doesn't show an unpaired surrogate prints the report.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from api_medic.cli import main as cli_main
from api_medic.core import runner
from api_medic.core.captured import CapturedDns, CapturedRequest
from api_medic.core.models import UNENCODABLE_TEXT
from api_medic.web import app as web_app

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "deploy" / "lambda"))

import handler  # sys.path manipulated above

REPORT = "<a report>"
VALIDATION = "<FastAPI's 422>"
UNREADABLE_JSON = "request body has a number too long or nesting too deep to read"


@dataclass(frozen=True)
class Live:
    """A GET /api/run sends, and the response the stubbed runner gets."""

    url: str = "https://api.example.com/"
    headers: dict[str, str] = field(default_factory=dict)
    status: int = 200
    response_headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


@dataclass(frozen=True)
class Body:
    """A request body for `path` on the two servers."""

    path: str
    text: str


@dataclass(frozen=True)
class Case:
    id: str
    # A HAR's JSON text (bytes: a file that isn't UTF-8), a curl command
    # (or another JSON value), a live run or a server request body.
    input: Any
    kind: Literal["har", "curl", "run", "body"]
    # REPORT, VALIDATION, or the message the 400 or exit 2 carries; a tuple
    # of them where the interpreter decides which, e.g. how deep json.loads
    # reads.
    expect: str | tuple[str, ...]
    differs: dict[str, str | tuple[str, ...]] = field(default_factory=dict)

    def entries(self) -> list[str]:
        if self.kind == "har":
            both = ["lambda", "serve", "cli"]
            return both if isinstance(self.input, bytes) else [*both, "lambda-string"]
        if self.kind == "curl":
            return (
                ["lambda", "serve", "cli"] if isinstance(self.input, str) else ["lambda", "serve"]
            )
        if self.kind == "run":
            return ["lambda", "serve", "cli", "cli-from-curl"]
        return ["lambda", "serve"]

    def expected(self, entry: str) -> str | tuple[str, ...]:
        return self.differs.get(entry, self.expect)


def _har(request: dict[str, Any] | None = None, **entry: Any) -> str:
    """The JSON text of a HAR whose one entry GETs https://api.example.com/,
    with `request`'s fields added and `entry`'s fields set."""
    req = {"method": "GET", "url": "https://api.example.com/", **(request or {})}
    return json.dumps({"log": {"entries": [{"request": req, **entry}]}})


def _har_response(status: int = 200, headers: dict[str, str] | None = None, **response: Any) -> str:
    response_headers = [{"name": k, "value": v} for k, v in (headers or {}).items()]
    return _har(response={"status": status, "headers": response_headers, **response})


def _jwt(payload: bytes) -> str:
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"eyJhbGciOiJIUzI1NiJ9.{b64}.sig"


def _bearer_cases(name: str, payload: bytes, expect: str) -> list[Case]:
    """A HAR, a curl command and a live run carrying Bearer _jwt(payload)."""
    header = {"Authorization": f"Bearer {_jwt(payload)}"}
    har_header = [{"name": k, "value": v} for k, v in header.items()]
    curl = shlex.join(
        ["curl", "https://api.example.com/", "-H", f"Authorization: {header['Authorization']}"]
    )
    return [
        Case(f"har-jwt-{name}", _har({"headers": har_header}), "har", expect),
        Case(f"curl-jwt-{name}", curl, "curl", expect),
        Case(f"run-jwt-{name}", Live(headers=header, status=401), "run", expect),
    ]


_JSON = {"Content-Type": "application/json"}
_DEEP = "[" * 100_000 + "]" * 100_000
_NINES = "9" * 4301
_CURL = "curl https://api.example.com/"

CASES: list[Case] = [
    # #51: exp and nbf claims no date can hold give the report of a token without them.
    *_bearer_cases("exp-nan", b'{"exp":NaN}', REPORT),
    *_bearer_cases("exp-minus-infinity", b'{"exp":-Infinity}', REPORT),
    *_bearer_cases("exp-minus-1e300", b'{"exp":-1e300}', REPORT),
    *_bearer_cases("exp-before-year-1", b'{"exp":-62135596801}', REPORT),
    *_bearer_cases("exp-1e400", b'{"exp":1e400}', REPORT),
    *_bearer_cases("nbf-nan", b'{"nbf":NaN}', REPORT),
    *_bearer_cases("nbf-infinity", b'{"nbf":Infinity}', REPORT),
    *_bearer_cases("nbf-1e20", b'{"nbf":1e20}', REPORT),
    *_bearer_cases("nbf-after-year-9999", b'{"nbf":253402300800}', REPORT),
    # #51: a payload that can't be parsed is not a JWT.
    *_bearer_cases("payload-not-utf-8", b"\xff", REPORT),
    *_bearer_cases("payload-4301-digit-int", b'{"exp":' + _NINES.encode() + b"}", REPORT),
    # 15,000 deep is past json's recursion limit and fits a curl command.
    *_bearer_cases("payload-nested-15000-deep", b"[" * 15_000 + b"]" * 15_000, REPORT),
    # #53: an unpaired surrogate.
    *_bearer_cases("sub-surrogate", b'{"sub":"\\ud800","exp":0}', UNENCODABLE_TEXT),
    Case(
        "har-header-surrogate",
        _har({"headers": [{"name": "X-Note", "value": "a\ud800b"}]}),
        "har",
        UNENCODABLE_TEXT,
    ),
    Case(
        "har-post-data-surrogate",
        _har({"method": "POST", "postData": {"text": "a\ud800"}}),
        "har",
        UNENCODABLE_TEXT,
    ),
    Case(
        "har-content-surrogate",
        _har_response(content={"text": "\ud800"}),
        "har",
        UNENCODABLE_TEXT,
    ),
    Case(
        "har-url-surrogate",
        _har({"url": "https://x.com/\ud800"}),
        "har",
        "1 validation error for RequestSummary",
    ),
    Case(
        "har-method-surrogate",
        _har({"method": "G\ud800T"}),
        "har",
        "1 validation error for RequestSummary",
    ),
    Case("curl-header-surrogate", f"{_CURL} -H 'X-Note: a\ud800b'", "curl", UNENCODABLE_TEXT),
    Case("curl-data-surrogate", f"{_CURL} -d 'a\ud800'", "curl", UNENCODABLE_TEXT),
    Case(
        "curl-url-surrogate",
        "curl 'https://x.com/\ud800'",
        "curl",
        "1 validation error for RequestSummary",
    ),
    Case(
        "body-kind-surrogate",
        Body("/api/analyze", '{"kind": "x\\ud800"}'),
        "body",
        "Unknown kind: 'x\\ud800'",
        # FastAPI's 422 no longer quotes the input.
        {"serve": VALIDATION},
    ),
    Case(
        "body-run-headers-surrogate",
        Body("/api/run", '{"method": "GET", "url": "https://x.com/", "headers": "x\\ud800"}'),
        "body",
        "'headers' must be a dict[str, str].",
        # FastAPI's 422 no longer quotes the input.
        {"serve": VALIDATION},
    ),
    # Invalid values nested 1,000 and 3,000 deep: FastAPI's 422 echoed the
    # value and recursed into it, a 500 from api-medic serve. Python 3.12's
    # json.loads reads about 10,000 levels; 3.10's and 3.11's stop near
    # 1,000, and then both servers answer their 400 for a body they can't
    # read.
    *[
        Case(
            f"body-{name}-nested-{depth}-deep",
            Body(path, text.replace("<deep>", "[" * depth + "]" * depth)),
            "body",
            (message, UNREADABLE_JSON),
            {"serve": (VALIDATION, "There was an error parsing the body")},
        )
        for depth in (1_000, 3_000)
        for name, path, text, message in [
            ("kind", "/api/analyze", '{"kind": <deep>}', "Unknown kind: [[["),
            ("har", "/api/analyze", '{"kind": "har", "har": <deep>}', "Not a HAR archive"),
            (
                "curl",
                "/api/analyze",
                '{"kind": "curl", "curl": <deep>}',
                "'curl' must be a string.",
            ),
            (
                "run-url",
                "/api/run",
                '{"method": "GET", "url": <deep>}',
                "Missing or empty 'url'.",
            ),
            (
                "run-headers",
                "/api/run",
                '{"method": "GET", "url": "https://x.com/", "headers": <deep>}',
                "'headers' must be a dict[str, str].",
            ),
            (
                "run-body",
                "/api/run",
                '{"method": "GET", "url": "https://x.com/", "body": <deep>}',
                "'body' must be a string or null.",
            ),
        ]
    ],
    # #53: rate-limit values no int comes from, and a Remaining/Limit ratio
    # too large for a float.
    Case(
        "har-rate-limit-4301-digits",
        _har_response(429, {"X-RateLimit-Remaining": _NINES, "Retry-After": _NINES}),
        "har",
        REPORT,
    ),
    Case(
        "har-retry-after-superscript", _har_response(429, {"Retry-After": "\u00b2"}), "har", REPORT
    ),
    Case(
        "har-remaining-ratio-1e400",
        _har_response(200, {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "1" + "0" * 400}),
        "har",
        REPORT,
    ),
    Case(
        "run-rate-limit-4301-digits",
        Live(status=429, response_headers={"Retry-After": _NINES}),
        "run",
        REPORT,
    ),
    Case(
        "run-remaining-ratio-1e400",
        Live(response_headers={"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "1" + "0" * 400}),
        "run",
        REPORT,
    ),
    # #53: JSON bodies Python can't read.
    *[
        Case(
            f"har-json-body-{name}",
            _har_response(200, _JSON, content={"text": text}),
            "har",
            REPORT,
        )
        for name, text in [
            ("nul", "{\x00}\x00"),
            ("4301-digit-int", _NINES),
            ("4301-digit-int-then-junk", _NINES + "x"),
            ("nested-100000-deep", _DEEP),
            ("nested-100000-deep-malformed", "[" * 100_000),
        ]
    ],
    *[
        Case(f"run-json-body-{name}", Live(response_headers=_JSON, content=content), "run", REPORT)
        for name, content in [
            ("latin-1", b'{"name": "caf\xe9"}'),
            ("utf-16", "{}".encode("utf-16")),
            ("4301-digit-int", _NINES.encode()),
            ("nested-100000-deep", _DEEP.encode()),
        ]
    ],
    # #55: curl commands too long to parse in time.
    Case(
        "curl-over-50000-characters",
        f"{_CURL} -d '" + "a" * 50_000 + "'",
        "curl",
        "Curl command is too long (limit: 50,000 characters).",
    ),
    Case(
        "curl-over-1000-arguments",
        _CURL + " -k" * 999,
        "curl",
        "Curl command has too many arguments (limit: 1,000).",
    ),
    Case(
        "curl-cookie-over-4096-characters",
        f"{_CURL} -H 'Cookie:" + "=" * 4_096 + "\U0001f600'",
        "curl",
        "Curl command's Cookie header is too long (limit: 4,096 characters).",
    ),
    # Request bodies json.loads can't read: the hosted demo answered 500, or
    # 400 with Python's message for a HAR sent as a string.
    *[
        case
        for name, value in [("4301-digit-int", _NINES), ("nested-100000-deep", _DEEP)]
        for case in [
            Case(
                f"har-{name}",
                _har()[:-1] + ', "x": ' + value + "}",
                "har",
                UNREADABLE_JSON,
                {"serve": "There was an error parsing the body"},
            ),
            Case(
                f"body-run-{name}",
                Body("/api/run", '{"method": "GET", "url": "https://x.com/", "x": ' + value + "}"),
                "body",
                UNREADABLE_JSON,
                {"serve": "There was an error parsing the body"},
            ),
        ]
    ],
    # HAR fields of the wrong type: AttributeError and OverflowError, a 500
    # or a traceback.
    *[
        Case(
            f"har-{field_name}-{type_name}",
            _har({"method": "POST", "postData": value})
            if field_name == "post-data"
            else _har_response(content=value)
            if field_name == "content"
            else _har(timings=value),
            "har",
            f"HAR entry[0] (https://api.example.com/): {message} must be an object.",
        )
        for field_name, message in [
            ("post-data", "request.postData"),
            ("content", "response.content"),
            ("timings", "timings"),
        ]
        for type_name, value in [("string", "x"), ("list", [1]), ("number", 5)]
    ],
    Case(
        "har-status-1e400",
        _har_response().replace('"status": 200', '"status": 1e400'),
        "har",
        "HAR entry's response.status is not an integer: inf",
    ),
    Case(
        "har-status-minus-1e400",
        _har_response().replace('"status": 200', '"status": -1e400'),
        "har",
        "HAR entry's response.status is not an integer: -inf",
    ),
    # HAR timings no finite float holds: -o html crashed on Infinity, and
    # parse_har on an integer too large for a float.
    Case(
        "har-timing-1e400",
        _har(timings={"dns": 0, "wait": 5}).replace('"dns": 0', '"dns": 1e400'),
        "har",
        REPORT,
    ),
    Case(
        "har-every-timing-1e400",
        _har(timings=dict.fromkeys(["dns", "connect", "ssl", "wait", "receive"], 0)).replace(
            ": 0", ": 1e400"
        ),
        "har",
        REPORT,
    ),
    Case("har-timing-400-digit-int", _har(timings={"dns": 10**400, "wait": 5}), "har", REPORT),
    # Finite phases whose sum isn't: the total showed as "inf s" / "inf ms".
    Case(
        "har-timing-sum-past-the-float-maximum",
        _har(timings={"dns": 1e308, "wait": 1e308}),
        "har",
        REPORT,
    ),
    # A live status outside 100-599, which h11 accepts: a 500 from both
    # /api/run routes and a traceback from run and from-curl.
    *[
        Case(
            f"run-status-{status}",
            Live(status=status),
            "run",
            REPORT
            if status <= 599
            else f"The server answered with status {status}, outside 100-599.",
        )
        for status in (200, 599, 600, 999)
    ],
    # A HAR parse_har rejects, and a request the runner refuses to send:
    # from-har and run printed a traceback.
    Case("har-not-a-har-archive", '{"foo": "bar"}', "har", "Not a HAR archive (missing 'log')."),
    Case("har-no-entries", '{"log": {"entries": []}}', "har", "HAR has no entries."),
    Case("har-not-json", "nope", "har", "Expecting value", {"serve": VALIDATION}),
    Case(
        "har-is-a-string-holding-a-surrogate",
        '"x\\ud800"',
        "har",
        "Not a HAR archive (missing 'log').",
        {"lambda": "Expecting value", "serve": VALIDATION},
    ),
    Case(
        "har-method-missing",
        json.dumps({"log": {"entries": [{"request": {"url": "https://api.example.com/"}}]}}),
        "har",
        "HAR entry[0] (https://api.example.com/): request.method is missing.",
    ),
    Case(
        # The message quotes the URL, which stderr can't print.
        "har-method-missing-url-surrogate",
        json.dumps({"log": {"entries": [{"request": {"url": "https://x.com/\ud800"}}]}}),
        "har",
        "HAR entry[0] (https://x.com/\ud800): request.method is missing.",
        {"serve": UNENCODABLE_TEXT, "cli": UNENCODABLE_TEXT},
    ),
    Case(
        "har-status-not-an-integer",
        _har(response={"status": "abc"}),
        "har",
        "HAR entry's response.status is not an integer: 'abc'",
    ),
    Case("har-status-999", _har_response(999), "har", "1 validation error for CapturedResponse"),
    Case(
        "har-not-utf-8",
        b'{"log": {"entries": [{"request": {"method": "GET", "url": "https://x.com/caf\xe9"}}]}}',
        "har",
        "'utf-8' codec can't decode byte 0xe9",
        # The Lambda decodes the body with errors="replace".
        {"lambda": REPORT, "serve": "There was an error parsing the body"},
    ),
    Case(
        "run-port-99999",
        Live(url="https://api.example.com:99999/"),
        "run",
        "port must be a whole number from 1 to 65535 (got '99999').",
    ),
    # A curl value that isn't a string: AttributeError or TypeError in
    # parse_curl, a 500 from the hosted demo. A har value that is neither a
    # string nor an object was already a 400.
    *[
        Case(f"curl-{name}", value, "curl", "'curl' must be a string.", {"serve": VALIDATION})
        for name, value in [("number", 5), ("true", True), ("list", [1]), ("object", {"a": 1})]
    ],
    *[
        Case(
            f"har-{name}", text, "har", "Not a HAR archive (missing 'log').", {"serve": VALIDATION}
        )
        for name, text in [("number", "5"), ("true", "true"), ("list", "[1]")]
    ],
    # The Cookie header a curl command carries is kept, and goes through the
    # same checks as every other header.
    Case("curl-cookie", f"{_CURL} -H 'Cookie: b=2; a=1; Path=/'", "curl", REPORT),
    Case("curl-cookie-surrogate", f"{_CURL} -H 'Cookie: a=\ud800'", "curl", UNENCODABLE_TEXT),
    Case("curl-empty", "   ", "curl", "Empty curl command."),
    Case(
        "curl-unparseable", "definitely not a curl command", "curl", "Could not parse curl command"
    ),
]


# --- Entry points ----------------------------------------------------------

_FORMATS = ("json", "terminal", "markdown", "html")


def _flat(text: str) -> str:
    """`text` without Rich's colour codes, error box and line wrapping."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[\u2500-\u257f]", " ", text).split())


def _lambda(path: str, body: str | bytes) -> str:
    if isinstance(body, bytes):
        event_body, is_base64 = base64.b64encode(body).decode("ascii"), True
    else:
        event_body, is_base64 = body, False
    event = {
        "requestContext": {"http": {"method": "POST", "path": path}},
        "rawPath": path,
        "body": event_body,
        "isBase64Encoded": is_base64,
    }
    result = handler.lambda_handler(event, None)
    status, payload = result["statusCode"], json.loads(result["body"])
    if status == 200:
        assert "findings" in payload, payload
        return REPORT
    assert status == 400, (status, payload)
    detail: str = payload["detail"]
    return detail


def _serve(path: str, body: str | bytes) -> str:
    client = TestClient(web_app.app, raise_server_exceptions=False)
    r = client.post(path, content=body, headers={"content-type": "application/json"})
    if r.status_code == 200:
        assert "findings" in r.json(), r.text
        return REPORT
    if r.status_code == 422:
        return VALIDATION
    assert r.status_code == 400, (r.status_code, r.text)
    detail: str = r.json()["detail"]
    return detail


def _cli(args: list[str]) -> dict[str, str]:
    """How `api-medic <args>` ends in each output format."""
    endings: dict[str, str] = {}
    for fmt in _FORMATS:
        result = CliRunner().invoke(cli_main.app, [*args, "-o", fmt])
        if result.exit_code == 0:
            if fmt == "json":
                assert "findings" in json.loads(result.stdout)
            assert result.stdout
            endings[fmt] = REPORT
        else:
            assert result.exit_code == 2 and isinstance(result.exception, SystemExit), (
                fmt,
                result.exit_code,
                repr(result.exception),
            )
            endings[fmt] = _flat(result.stderr)
    return endings


def _ends_as_expected(got: str, want: str | tuple[str, ...]) -> bool:
    if isinstance(want, tuple):
        return any(_ends_as_expected(got, one) for one in want)
    if want in (REPORT, VALIDATION):
        return got == want
    return got not in (REPORT, VALIDATION) and want in got


def _send(case: Case, entry: str, tmp_path: Path) -> str | dict[str, str]:
    if case.kind == "har":
        har = case.input
        if entry == "cli":
            path = tmp_path / "in.har"
            path.write_bytes(har if isinstance(har, bytes) else har.encode("utf-8"))
            return _cli(["from-har", str(path)])
        if entry == "lambda-string":
            return _lambda("/api/analyze", json.dumps({"kind": "har", "har": har}))
        if isinstance(har, bytes):
            body: str | bytes = b'{"kind": "har", "har": ' + har + b"}"
        else:
            body = '{"kind": "har", "har": ' + har + "}"
        return (_lambda if entry == "lambda" else _serve)("/api/analyze", body)
    if case.kind == "curl":
        if entry == "cli":
            return _cli(["from-curl", case.input, "--no-execute"])
        body = json.dumps({"kind": "curl", "curl": case.input})
        return (_lambda if entry == "lambda" else _serve)("/api/analyze", body)
    if case.kind == "run":
        live: Live = case.input
        if entry == "cli":
            headers = [arg for k, v in live.headers.items() for arg in ("-H", f"{k}: {v}")]
            return _cli(["run", live.url, *headers])
        if entry == "cli-from-curl":
            headers = [arg for k, v in live.headers.items() for arg in ("-H", f"{k}: {v}")]
            return _cli(["from-curl", shlex.join(["curl", live.url, *headers])])
        body = json.dumps({"method": "GET", "url": live.url, "headers": live.headers})
        return (_lambda if entry == "lambda" else _serve)("/api/run", body)
    request: Body = case.input
    return (_lambda if entry == "lambda" else _serve)(request.path, request.text)


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Give every runner call the case's response, with no DNS lookup, TLS
    probe or network."""

    def getaddrinfo(host: str, *a: Any, **kw: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    def resolve_dns(host: str, timeout: float = 5.0) -> tuple[CapturedDns, float]:
        return CapturedDns(records=["93.184.216.34"]), 0.0

    def probe_tls(host: str, port: int, *a: Any, **kw: Any) -> tuple[None, float]:
        return None, 0.0

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(runner, "_resolve_dns", resolve_dns)
    monkeypatch.setattr(runner, "_probe_tls", probe_tls)

    def stub(live: Live) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(live.status, headers=live.response_headers, content=live.content)

        def run(*args: Any, **kwargs: Any) -> CapturedRequest:
            return runner.run(*args, **kwargs, transport=httpx.MockTransport(respond))

        monkeypatch.setattr(handler, "run_request", run)
        monkeypatch.setattr(web_app, "run_request", run)
        monkeypatch.setattr(cli_main, "runner_run", run)

    return stub


@pytest.mark.parametrize(
    ("case", "entry"),
    [
        pytest.param(case, entry, id=f"{case.id}@{entry}")
        for case in CASES
        for entry in case.entries()
    ],
)
def test_ends_in_a_report_or_an_error_carrying_a_message(
    case: Case, entry: str, tmp_path: Path, stub_runner: Any
) -> None:
    if isinstance(case.input, Live):
        stub_runner(case.input)
    want = case.expected(entry)
    got = _send(case, entry, tmp_path)
    if isinstance(got, str):
        assert _ends_as_expected(got, want), (got, want)
        return
    assert isinstance(want, str)
    assert _ends_as_expected(got["json"], _flat(want)), (got["json"], want)
    for fmt in _FORMATS[1:]:
        if want == UNENCODABLE_TEXT and got[fmt] == REPORT:
            continue  # this format doesn't show the surrogate
        assert _ends_as_expected(got[fmt], _flat(want)), (fmt, got[fmt], want)
