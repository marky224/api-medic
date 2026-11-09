"""Parse external request representations into CapturedRequest.

Used by:
  * the local web UI's POST /api/analyze
  * the hosted demo's Lambda /api/analyze
  * the CLI's `from-har` and `from-curl` commands (Phase 4)

Doesn't execute anything — just normalises what the user provided. For live
execution see `core.runner`.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import re
import shlex
from collections.abc import Iterator
from typing import Any

import uncurl  # type: ignore[import-untyped]

from .captured import CapturedRequest, CapturedResponse
from .models import TimingBreakdown, UnencodableTextError

# HAR `httpVersion` strings vary by browser: Chromium writes 'http/2.0' lowercase
# with .0, Firefox writes 'HTTP/2.0' uppercase with .0, and some tools use the
# ALPN identifier 'h2'. httpx's response.http_version is always 'HTTP/1.1' or
# 'HTTP/2' (uppercase, no .0 on h2). Normalising on parse keeps the rendered
# Report's Protocol field visually consistent regardless of which surface
# produced it. Unknown values pass through unchanged (key-missing in this map).
_HTTP_VERSION_NORMALIZATIONS = {
    "http/1.0": "HTTP/1.0",
    "http/1.1": "HTTP/1.1",
    "http/2": "HTTP/2",
    "http/2.0": "HTTP/2",
    "h2": "HTTP/2",
    "http/3": "HTTP/3",
    "http/3.0": "HTTP/3",
    "h3": "HTTP/3",
}

# uncurl splits the command with shlex, reads the arguments with argparse and
# reads each Cookie header value with http.cookies.SimpleCookie. Each slows
# down with a square: shlex with a token's length, argparse with the number
# of options, SimpleCookie with the value's length. Measured locally, one
# 1,000,000-character token took over 10 seconds, 33,323 -k flags (100,000
# characters) 21 seconds and a 40,000-character command whose Cookie value is
# a=a=..." 18 seconds. The hosted demo's Lambda, with its 30-second timeout,
# runs slower.
#
# A command that passes these checks is read by argparse twice: once to find
# its Cookie header values, then in uncurl. Of the accepted commands measured
# locally through the Lambda handler, the slowest were one 50,000-character
# -kkk...kHX: token ending in an emoji, 0.8 s, and that token shortened to
# carry a 4,096-character ===... Cookie value ending in an emoji, 1.2 s. The
# emoji makes Python store the string at 4 bytes a character. Under a 14.5%
# CPU quota, a local stand-in for the CPU share of the demo's 256 MB Lambda,
# they took 8.3 s and 11.5 s, and the token at 100,000 characters 26.9 s,
# which is why MAX_CURL_CHARS is 50,000.
MAX_CURL_CHARS = 50_000
MAX_CURL_ARGS = 1_000
MAX_CURL_COOKIE_CHARS = 4_096

# uncurl reads -b as --data-binary and doesn't know --cookie or -c, so
# parse_curl takes curl's cookie options out of the command first. curl reads
# a -b/--cookie value holding "=" as cookies to send, and any other value as
# a file to read them from ("-" is stdin, and "@" before a name makes it a
# file); -c/--cookie-jar names a file to write them to. api-medic reads and
# writes no files.
COOKIE_FILE = (
    "Curl command reads cookies from a file (-b/--cookie with a file name or -); "
    "api-medic reads no files. Give the cookies as -b 'NAME=VALUE'."
)
COOKIE_JAR = (
    "Curl command writes cookies to a file (-c/--cookie-jar); "
    "api-medic writes no files. Leave it out."
)
# The flags uncurl reads without a value: argparse reads -kb as -k and then
# -b, as curl does. A token like -Lb, which curl also reads as -L and -b,
# is one uncurl rejects.
_CURL_FLAGS = "iks"
# curl refuses one given a value after "=", as in -k=d x, and so does
# argparse from Python 3.11 on; parse_curl refuses it before argparse reads
# it, so every Python does.
_FLAG_WITH_A_VALUE = re.compile(rf"-[{_CURL_FLAGS}]+=")
# The options uncurl reads with a value, -b aside: the short ones, and each
# long one in every spelling argparse reads, as it takes an abbreviation no
# other option starts with (--hea is --header, --data-b is --data-binary,
# but --dat is refused). curl reads the argument after one as its value
# whatever it looks like, so parse_curl reads no option there.
_CURL_VALUE_FLAGS = "dHuX"
_SHORT_OPTION = re.compile(rf"-([{_CURL_FLAGS}]*)([bc{_CURL_VALUE_FLAGS}])(.*)", re.DOTALL)
_LONG_VALUE_OPTIONS = frozenset(
    full[:n]
    # Each option, and the length of its shortest spelling.
    for full, shortest in [
        ("--data", 6),
        ("--data-binary", 8),
        ("--data-raw", 8),
        ("--header", 5),
        ("--user", 3),
    ]
    for n in range(shortest, len(full) + 1)
)
_LONG_COOKIE_OPTIONS = {"--cookie": "b", "--cookie-jar": "c"}
# curl reads --url VALUE as a URL to fetch, as it reads one given without an
# option, and fetches every URL it is given; uncurl doesn't know --url.
MANY_URLS = (
    "Curl command gives more than one URL (--url); api-medic analyzes one request. Leave one URL."
)
# curl's URL globbing reads [ ] { } in a URL, and a backslash before one
# makes it a plain character, which curl sends without the backslash.
# Chrome's Copy as cURL (bash) writes one before each. uncurl doesn't know
# -g/--globoff, which turns globbing off.
_GLOB_ESCAPE = re.compile(r"\\(?=[][{}])")
# uncurl reads -u/--user as credentials, which parse_curl doesn't send, and
# a -d/--data or --data-binary value as the body; curl reads one starting
# with "@" from the file it names ("@-" is stdin), and a --data-raw value as
# given.
USER_OPTION = (
    "Curl command sends credentials with -u/--user, which api-medic doesn't read. "
    "Give them as -H 'Authorization: ...' instead."
)
DATA_FILE = (
    "Curl command reads data from a file (-d/--data/--data-binary with @); "
    "api-medic reads no files. Give the data as --data-raw '...'."
)
# curl sends -H 'Name;' as the header Name with no value, and Chrome's Copy
# as cURL (bash) writes a header with an empty value that way. uncurl splits
# each header at its first colon.
_EMPTY_HEADER = re.compile(r"[^-:;][^:;]*;")
# Chrome's Copy as cURL (bash) writes a value holding ', ! or a control
# character as $'...', bash's ANSI-C quoting, with the escapes \\, \', \n,
# \r, and \u and 4 hex digits. bash keeps the backslash before a character
# it reads no escape from. It reads other escapes too, but cuts the value
# at U+0000 and reads a surrogate by the locale; api-medic reads those
# five only.
ANSI_ESCAPE = (
    "Could not parse curl command: a $'...' string uses an escape api-medic doesn't read. "
    "It reads \\\\, \\', \\n, \\r and \\u with 4 hex digits (not \\u0000 or \\ud800-\\udfff)."
)
_ANSI_C_STRING = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
_ANSI_C_ESCAPE = re.compile(r"\\(u[0-9A-Fa-f]{4}|.)", re.DOTALL)
_ANSI_C_ESCAPES = {"\\": "\\", "'": "'", "n": "\n", "r": "\r"}
# The characters after a backslash that bash reads an escape from.
_BASH_ESCAPES = frozenset("abeEfnrtv\\'\"?01234567xuUc")
# What a command written for another shell has, which parse_curl names when
# it can't parse the command. It looks for them in the command as given,
# without tracking quotes: Windows cmd writes a " inside a value as ^\^",
# which a bash quote tracker misreads. cmd's quotes also leave a ^ before
# the URL, which no URL starts with.
CMD_LINE_END = (
    "Could not parse curl command: a line ends in ^, as a Windows cmd command continues "
    "a line. api-medic reads curl commands as bash does: copy the request as cURL (bash) "
    "instead."
)
CMD_QUOTES = (
    'Could not parse curl command: it uses ^" quotes, as a Windows cmd command does. '
    "api-medic reads curl commands as bash does: copy the request as cURL (bash) instead."
)
POWERSHELL_LINE_END = (
    "Could not parse curl command: a line ends in `, as a PowerShell command continues a "
    "line. api-medic reads curl commands as bash does: end each line in \\ instead."
)
SPACE_AFTER_BACKSLASH = (
    "Could not parse curl command: a line ends in a backslash followed by spaces, so the "
    "line doesn't continue. Remove the spaces after the backslash."
)
_OTHER_SHELLS = [
    (re.compile(r"\^\r?\n"), CMD_LINE_END),
    (re.compile(r'\^"'), CMD_QUOTES),
    (re.compile(r"`\r?\n"), POWERSHELL_LINE_END),
    (re.compile(r"\\[ \t]+\r?\n"), SPACE_AFTER_BACKSLASH),
]

UNREADABLE_JSON = "request body has a number too long or nesting too deep to read"
"""What parse_har raises, and the hosted demo's handler answers for a request
body, when json.loads can't read valid JSON: an integer over Python's
4,300-digit conversion limit, or nesting past the recursion limit (about
10,000 levels on Python 3.12, 3,000 on Windows)."""


def _normalize_http_version(raw: Any) -> str:
    if not isinstance(raw, str):
        return "HTTP/1.1"
    stripped = raw.strip()
    if not stripped:
        return "HTTP/1.1"
    return _HTTP_VERSION_NORMALIZATIONS.get(stripped.lower(), stripped)


def parse_har(raw: str | dict[str, Any]) -> CapturedRequest:
    """Parse a HAR 1.2 archive's first entry into a CapturedRequest.

    Multi-entry HARs are common (a full session capture); for v1 we analyse
    the first entry only.
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise
    except (ValueError, RecursionError) as e:
        raise ValueError(UNREADABLE_JSON) from e
    if not isinstance(data, dict) or "log" not in data:
        raise ValueError("Not a HAR archive (missing 'log').")
    log = data["log"]
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("HAR has no entries.")
    entry = entries[0]
    if not isinstance(entry, dict) or "request" not in entry:
        raise ValueError("HAR entry is missing 'request'.")

    request = entry["request"]
    if not isinstance(request, dict):
        raise ValueError("HAR entry's 'request' must be an object.")

    # Best-effort URL extraction so field-error messages can identify which
    # captured request failed. Real-world HARs have many entries; v1 only
    # parses entries[0] but the URL is what tells the user *which* request
    # that was. Degrades to "HAR entry[0]" when url itself is the bad field.
    maybe_url = request.get("url")
    label = (
        f"HAR entry[0] ({maybe_url})"
        if isinstance(maybe_url, str) and maybe_url
        else "HAR entry[0]"
    )

    if "method" not in request:
        raise ValueError(f"{label}: request.method is missing.")
    method = request["method"]
    if not isinstance(method, str):
        raise ValueError(f"{label}: request.method must be a string, got {type(method).__name__}.")
    if not method:
        raise ValueError(f"{label}: request.method is empty.")

    if "url" not in request:
        raise ValueError("HAR entry[0]: request.url is missing.")
    url = request["url"]
    if not isinstance(url, str):
        raise ValueError(f"HAR entry[0]: request.url must be a string, got {type(url).__name__}.")
    if not url:
        raise ValueError("HAR entry[0]: request.url is empty.")

    request_headers = _har_headers(request.get("headers"))
    body_text = _har_object(request.get("postData"), label, "request.postData").get("text", "")
    body = _utf8(body_text) if isinstance(body_text, str) and body_text else b""

    captured_response: CapturedResponse | None = None
    response_obj = entry.get("response")
    if isinstance(response_obj, dict) and response_obj.get("status"):
        resp_headers = _har_headers(response_obj.get("headers"))
        content = _har_object(response_obj.get("content"), label, "response.content")
        resp_body_text = content.get("text", "")
        resp_body = (
            _utf8(resp_body_text) if isinstance(resp_body_text, str) and resp_body_text else b""
        )
        try:
            status_code = int(response_obj["status"])
        except (TypeError, ValueError, OverflowError) as e:
            # OverflowError: int() of Infinity, which JSON's 1e400 reads as.
            raise ValueError(
                f"HAR entry's response.status is not an integer: {response_obj['status']!r}"
            ) from e
        status_text_raw = response_obj.get("statusText")
        captured_response = CapturedResponse(
            status_code=status_code,
            status_text=str(status_text_raw) if isinstance(status_text_raw, str) else "",
            headers=resp_headers,
            body=resp_body,
            protocol=_normalize_http_version(response_obj.get("httpVersion")),
        )

    timing = _timing_from_har(_har_object(entry.get("timings"), label, "timings"))

    return CapturedRequest(
        method=method.upper(),
        url=url,
        headers=request_headers,
        body=body,
        response=captured_response,
        timing=timing,
        source="har",
    )


def parse_curl(curl_str: str) -> CapturedRequest:
    """Parse a curl command string into a CapturedRequest.

    The curl command describes a request only — the resulting CapturedRequest
    has no `response`. Pair with `core.runner` to actually execute it. Its
    Cookie headers are kept as given, joined with "; " into one. Without
    one, its -b/--cookie cookies are the Cookie header, joined with ";", as
    curl sends them; a -b/--cookie file or a -c/--cookie-jar is a
    ValueError. A command on several lines, each but the last ending in a
    backslash, is read as one, as bash reads it (see _normalize_bash); one
    written for Windows cmd or PowerShell is a ValueError naming that.
    """
    # The limit counts the command as given; joining its lines only
    # shortens it.
    if len(curl_str) > MAX_CURL_CHARS:
        raise ValueError(f"Curl command is too long (limit: {MAX_CURL_CHARS:,} characters).")
    try:
        command = _normalize_bash(curl_str)
    except ValueError as e:
        raise _parse_error(curl_str, str(e)) from e
    if not command.strip():
        raise ValueError("Empty curl command.")
    # uncurl splits the command again, the same way.
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise _parse_error(curl_str, f"Could not parse curl command: {e}") from e
    if len(args) > MAX_CURL_ARGS:
        raise ValueError(f"Curl command has too many arguments (limit: {MAX_CURL_ARGS:,}).")
    uncurl_args, cookie_options = _curl_options(args)
    with _uncurl_errors(curl_str):
        if any(_FLAG_WITH_A_VALUE.match(arg) for arg in uncurl_args):
            uncurl.api.parser.error("a flag with a value")
        # The parser uncurl.parse_context reads the arguments with.
        parsed = uncurl.api.parser.parse_args(uncurl_args)
        # argparse takes an argument starting with "-" as the URL when it is
        # a negative number, such as -1 (curl's --tlsv1), holds a space, is
        # "-" or comes after "--"; curl reads most as options. In curl -- --
        # it takes the second "--" as the URL "--", or on older Pythons, such
        # as 3.11 and 3.12.3, as the URL [].
        if parsed.url == [] or parsed.url.startswith("-"):
            uncurl.api.parser.error("no URL")
    if parsed.user != ():  # uncurl's default
        raise ValueError(USER_OPTION)
    # The last -d/--data value, the one uncurl reads.
    if parsed.data and parsed.data.startswith("@"):
        raise ValueError(DATA_FILE)
    cookie_values = _cookie_values(parsed.header)
    # The -b/--cookie cookies count even when a Cookie header replaces them.
    if sum(len(value) for value in [*cookie_values, *cookie_options]) > MAX_CURL_COOKIE_CHARS:
        raise ValueError(
            "Curl command's Cookie header is too long "
            f"(limit: {MAX_CURL_COOKIE_CHARS:,} characters)."
        )
    with _uncurl_errors(curl_str):
        # uncurl splits the command with shlex as well, so it gets the
        # arguments written again when _curl_options changed them.
        ctx = uncurl.parse_context(command if uncurl_args == args else shlex.join(uncurl_args))
    url = _GLOB_ESCAPE.sub("", ctx.url)
    if url.startswith("^"):
        raise ValueError(CMD_QUOTES)

    method = (ctx.method or "GET").upper()
    headers = dict(ctx.headers) if ctx.headers else {}
    # uncurl moves every Cookie header into ctx.cookies, read with
    # SimpleCookie, which reorders the cookies and drops attributes. Send
    # them as the command gave them, stripped like uncurl strips the others.
    # curl sends the -b/--cookie cookies, joined with ";", only when there
    # is no Cookie header; an empty one (-H 'Cookie:') sends no cookies at
    # all.
    values, sep = (cookie_values, "; ") if cookie_values else (cookie_options, ";")
    cookie = sep.join(value.strip() for value in values if value.strip())
    if cookie:
        headers["Cookie"] = cookie
    body = _utf8(ctx.data) if ctx.data else b""

    return CapturedRequest(
        method=method,
        url=url,
        headers=headers,
        body=body,
        response=None,
        timing=TimingBreakdown(),
        source="curl",
    )


def _normalize_bash(command: str) -> str:
    """`command` as shlex reads it the way bash does: without its line
    continuations, and with each $'...' string as the single-quoted string
    of its value. A backslash before a line break goes with the line
    break, as bash removes it before splitting the command into words,
    except inside single quotes and when the backslash is itself escaped.
    bash joins a backslash before LF only; this joins one before CRLF too.
    Quotes are tracked as shlex tracks them."""
    if "\\\n" not in command and "\\\r\n" not in command and "$'" not in command:
        return command
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        char = command[i]
        if quote == "'":
            if char == "'":
                quote = ""
        elif char == "\\":
            if command.startswith("\n", i + 1):
                i += 2
                continue
            if command.startswith("\r\n", i + 1):
                i += 3
                continue
            # A backslash escapes the next character, in or out of double
            # quotes.
            out.append(command[i : i + 2])
            i += 2
            continue
        elif char == '"':
            quote = "" if quote else '"'
        elif char == "$" and not quote and command.startswith("'", i + 1):
            match = _ANSI_C_STRING.match(command, i)
            if not match:
                raise ValueError("Could not parse curl command: No closing quotation")
            out.append(shlex.quote(_ansi_c(match[1])))
            i = match.end()
            continue
        elif char == "'" and not quote:
            quote = "'"
        out.append(char)
        i += 1
    return "".join(out)


def _ansi_c(text: str) -> str:
    """The value of the $'...' string holding `text`, as bash reads the
    escapes Chrome writes; another escape bash reads is a ValueError."""

    def unescape(match: re.Match[str]) -> str:
        escape = match[1]
        if escape in _ANSI_C_ESCAPES:
            return _ANSI_C_ESCAPES[escape]
        if len(escape) == 5:  # u and 4 hex digits
            code = int(escape[1:], 16)
            if code and not 0xD800 <= code <= 0xDFFF:
                return chr(code)
        elif escape not in _BASH_ESCAPES:
            return match[0]
        raise ValueError(ANSI_ESCAPE)

    return _ANSI_C_ESCAPE.sub(unescape, text)


def _parse_error(curl_str: str, message: str) -> ValueError:
    """The error for `curl_str`, a command as given that can't be parsed:
    what in it was written for another shell, if anything, or `message`."""
    for pattern, other_shell in _OTHER_SHELLS:
        if pattern.search(curl_str):
            return ValueError(other_shell)
    return ValueError(message)


@contextlib.contextmanager
def _uncurl_errors(curl_str: str) -> Iterator[None]:
    """uncurl's and its argparse's errors as ValueError, naming what
    `curl_str`, the command as given, has from another shell."""
    try:
        # argparse prints why it rejected the command to stderr, quoting
        # arguments it didn't recognize (cookie values, bodies), and its help,
        # for -h or --help, to stdout. Lambda sends both to CloudWatch, where
        # request data must not go, and from-curl would print the help.
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            yield
    except SystemExit as e:
        # uncurl uses argparse, which calls sys.exit() on parse failure.
        raise _parse_error(curl_str, "Could not parse curl command (argparse rejected it).") from e
    except Exception as e:
        raise _parse_error(curl_str, f"Could not parse curl command: {e}") from e


def _curl_options(args: list[str]) -> tuple[list[str], list[str]]:
    """`args` as uncurl should read them, and their -b/--cookie values.

    Takes out curl's -b/--cookie options, and its --url, whose value goes
    in as the URL uncurl reads, and gives the value of an -H or --header
    written Name; as Name:, which uncurl reads as Name with no value.
    Reads -b VALUE, -bVALUE (whose "=" curl keeps: -b=a=1 is "=a=1"),
    -kb VALUE and -kbVALUE, --cookie VALUE and --cookie=VALUE, where VALUE
    is the next argument whatever it starts with, as curl reads it, and
    --url VALUE and --url=VALUE, unless VALUE starts with "-". The value of
    each other option uncurl reads with one, as in -d VALUE, -kd VALUE,
    --data-b VALUE or --heade VALUE, and a --url value starting with "-",
    are left as given for uncurl, which rejects one that looks like an
    option; none is read as one of these options. A short one's value
    attached after "=", as in -d=x or -kH=a: b, goes to uncurl as the
    argument after it (-d =x), so argparse keeps the "=", as curl does,
    where it would drop it. --head, which curl reads as -I and argparse as
    --header, goes to uncurl as -I, which it doesn't know, and so does
    --head=VALUE, which curl refuses. An option with no argument after it,
    and anything after "--", are left for uncurl to reject. A cookie file,
    a -c/--cookie-jar, more than one --url or a --data-binary file is a
    ValueError.
    """
    kept: list[str] = []
    cookies: list[str] = []
    urls: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":  # argparse reads the rest as positional arguments
            kept += args[i - 1 :]
            break
        if arg in ("-H", "--header") and i < len(args) and _EMPTY_HEADER.fullmatch(args[i]):
            kept += [arg, args[i][:-1] + ":"]
            i += 1
            continue
        name, eq, attached = arg.partition("=")
        # argparse reads --data-b and the longer abbreviations as
        # --data-binary, whose value it stores where it stores --data-raw's.
        if name.startswith("--data-b") and "--data-binary".startswith(name):
            data = attached if eq else args[i] if i < len(args) else ""
            if data.startswith("@"):
                raise ValueError(DATA_FILE)
        if name == "--url":
            url = attached if eq else args[i] if i < len(args) else None
            if url is not None and not url.startswith("-"):
                if not eq:
                    i += 1
                urls.append(url)
                continue
        # curl's -I, which takes no value and which uncurl rejects.
        if name == "--head":
            kept.append("-I")
            continue
        if name in _LONG_COOKIE_OPTIONS:
            flags, option, value = "", _LONG_COOKIE_OPTIONS[name], attached if eq else None
        elif name in _LONG_VALUE_OPTIONS or name == "--url":
            flags, option, value = "", "", attached if eq else None
        elif match := _SHORT_OPTION.fullmatch(arg):
            flags, option, value = match[1], match[2], match[3] or None
        else:
            kept.append(arg)
            continue
        if option not in ("b", "c"):
            if option and value and value.startswith("="):
                # argparse would drop the "=", which curl keeps.
                kept += ["-" + flags + option, value]
            else:
                kept.append(arg)
            if value is None and i < len(args) and args[i] != "--":
                kept.append(args[i])
                i += 1
            continue
        if value is None:
            if i == len(args):
                kept.append(arg)
                continue
            value = args[i]
            i += 1
        if option == "c":
            raise ValueError(COOKIE_JAR)
        if "=" not in value or value.startswith("@"):
            raise ValueError(COOKIE_FILE)
        if flags:
            kept.append("-" + flags)
        cookies.append(value)
    if len(urls) > 1:
        raise ValueError(MANY_URLS)
    # uncurl reads the first argument as the command and the next as the URL.
    kept[1:1] = urls
    return kept, cookies


def _cookie_values(headers: list[str]) -> list[str]:
    """The values of the headers uncurl reads with SimpleCookie: those whose
    key, lowercased and stripped of "$", is "cookie"."""
    values = []
    for header in headers:
        key, _, value = header.partition(":")
        if key.lower().strip("$") == "cookie":
            values.append(value)
    return values


def _utf8(text: str) -> bytes:
    """`text` as UTF-8. JSON input can carry an unpaired surrogate ("\\ud800")
    that UTF-8 can't encode; that is UnencodableTextError."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise UnencodableTextError from e


def _har_object(value: Any, label: str, name: str) -> dict[str, Any]:
    """A HAR field that holds an object: `value`, or {} when it is absent or
    falsy (null, "", [], 0), as before. Anything else is a ValueError naming
    the entry and the field."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label}: {name} must be an object.")
    return value


def _har_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for h in raw:
        if isinstance(h, dict) and "name" in h and "value" in h:
            out[str(h["name"])] = str(h["value"])
    return out


def _timing_from_har(t: dict[str, Any]) -> TimingBreakdown:
    """HAR timings are in ms; -1 means 'not measured', and so does a value
    no finite float holds: Infinity (JSON's 1e400) or an integer too large
    for a float. So does the total when the measured phases add up past the
    float maximum."""

    def _opt(v: Any) -> float | None:
        if isinstance(v, (int, float)) and v >= 0:
            try:
                ms = float(v)
            except OverflowError:
                return None
            if math.isfinite(ms):
                return ms
        return None

    dns = _opt(t.get("dns"))
    connect = _opt(t.get("connect"))
    ssl_ = _opt(t.get("ssl"))
    wait = _opt(t.get("wait"))
    receive = _opt(t.get("receive"))

    parts: list[float] = [v for v in (dns, connect, ssl_, wait, receive) if v is not None]
    total: float | None = sum(parts) if parts else None
    if total is not None and not math.isfinite(total):
        total = None  # finite phases that add up past the float maximum

    return TimingBreakdown(
        dns_ms=dns,
        connect_ms=connect,
        tls_ms=ssl_,
        ttfb_ms=wait,
        download_ms=receive,
        total_ms=total,
    )
