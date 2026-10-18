// Masks credentials in a captured HAR entry before it leaves the browser.
//
// Rule of thumb: keep exactly what the analyzer's checks read
// (src/api_medic/core/checks/), mask everything that looks secret, and keep
// every masked value's length. tests/unit/test_redaction_parity.py runs the
// analyzer on masked and unmasked fixtures to prove the findings match, so
// keep it passing when this file changes. What the checks need:
//
//   * auth.jwt.expired / not_yet_valid: an Authorization header with a
//     "Bearer"/"bearer" scheme, a token of three dot-separated parts, and a
//     middle part that still decodes to the exp / nbf claims.
//   * auth.missing: the Authorization header stays present (or absent).
//   * auth.header.whitespace: every whitespace character of the
//     Authorization value, by Python's definition, stays where it was.
//   * body.*: a response body keeps its UTF-8 byte length and stays valid
//     (or invalid) JSON.
//   * http.cors.misconfigured, rate_limit.*: Origin,
//     Access-Control-Allow-Origin, Retry-After and X-RateLimit-* are never
//     touched.

type Json = Record<string, unknown>;

export const JWT_SIGNATURE_MARKER = "REDACTED";

// Whitespace to Python or JavaScript. Python's \s (str.isspace) adds
// \x1c-\x1f and \x85 to JavaScript's \s; JavaScript's adds ﻿.
const WS = "\\s\\x1c-\\x1f\\x85";
const WS_CHAR = new RegExp(`^[${WS}]$`, "u");
const WS_CHARS = new RegExp(`[${WS}]`, "gu");
const NON_WS_RUN = new RegExp(`[^${WS}]+`, "gu");
// Mirrors auth.py's _BEARER_RE: ^\s*[Bb]earer\s+(.+?)\s*$
const BEARER = new RegExp(
  `^([${WS}]*)([Bb]earer)([${WS}]+)([\\s\\S]+?)([${WS}]*)$`,
  "u",
);
const FIRST_WORD = new RegExp(`^([${WS}]*)([^${WS}]+)([\\s\\S]*)$`, "u");
const JWT_HEADER_PART = /^[A-Za-z0-9_-]+={0,2}$/;

// Kept in plain text before a masked Authorization or Proxy-Authorization
// credential. An unknown first word may be the secret itself, so it is
// masked along with the rest.
const AUTH_SCHEMES = new Set([
  "aws4-hmac-sha256",
  "apikey",
  "basic",
  "bearer",
  "concealed",
  "digest",
  "dpop",
  "gnap",
  "hawk",
  "hoba",
  "jwt",
  "key",
  "mutual",
  "negotiate",
  "ntlm",
  "oauth",
  "privatetoken",
  "scram-sha-1",
  "scram-sha-256",
  "ssws",
  "token",
  "vapid",
]);

// The only JWT claims the checks read (exp, nbf) plus the subject, which
// their evidence shows.
const KEPT_CLAIMS = new Set(["exp", "nbf", "sub"]);

// Names matched after lowercasing and dropping "-", "_", "." and spaces. A
// name that ends in "key" is secret too (accessKey, subscription-key,
// X-Master-Key), and so, in URLs and form fields, is one that ends in
// "code" (see isSecretParam).
const SECRET_NAMES = new Set(["auth", "jwt", "otp", "pass", "pwd", "sid", "sig"]);
const SECRET_NAME_PARTS = [
  "apikey",
  "assertion",
  "credential",
  "passphrase",
  "passwd",
  "password",
  "privatekey",
  "secret",
  "session",
  "signature",
  "token",
  "verifier",
];
// Header names secret by exact name only (headers don't get the ends-in-code
// rule).
const SECRET_HEADERS = new Set(["code", "dpop"]);
// Never masked by name: CORS headers (Access-Control-Allow-Credentials would
// match "credential"), WebSocket handshake nonces and auth challenges.
const NEVER_SECRET_HEADER =
  /^(access-control-|sec-websocket-)|^(www|proxy)-authenticate$/;
const URL_HEADERS = new Set([":path", "content-location", "location", "referer"]);

export function redactEntry(entry: unknown): unknown {
  if (!isRecord(entry)) return entry;
  // Work on a copy: the panel keeps the captured entry for its list and for
  // Re-run. The reviver drops browser-specific fields (_initiator,
  // _webSocketMessages, ...), which the analyzer never reads and which can
  // carry URLs and messages with tokens in them.
  const copy = JSON.parse(JSON.stringify(entry), (key, value: unknown) =>
    key.startsWith("_") ? undefined : value,
  ) as Json;

  const request = copy.request;
  if (isRecord(request)) {
    if (typeof request.url === "string") request.url = maskUrl(request.url);
    redactHeaders(request.headers);
    redactNamedValues(request.queryString);
    redactCookies(request.cookies);
    const postData = request.postData;
    if (isRecord(postData)) {
      if (typeof postData.text === "string") {
        postData.text = maskBody(postData.text, postData.mimeType);
      }
      redactNamedValues(postData.params);
    }
  }

  const response = copy.response;
  if (isRecord(response)) {
    redactHeaders(response.headers);
    redactCookies(response.cookies);
    if (typeof response.redirectURL === "string") {
      response.redirectURL = maskUrl(response.redirectURL);
    }
    const content = response.content;
    if (
      isRecord(content) &&
      typeof content.text === "string" &&
      content.encoding !== "base64"
    ) {
      content.text = maskBody(content.text, content.mimeType);
    }
  }
  return copy;
}

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Every non-whitespace character becomes "*"; whitespace stays in place.
// Steps by code point so the length matches Python's len().
function mask(value: string): string {
  let out = "";
  for (const ch of value) out += WS_CHAR.test(ch) ? ch : "*";
  return out;
}

// One "*" per UTF-8 byte, so a masked body keeps its byte length.
function maskBytes(value: string): string {
  let out = "";
  for (const ch of value) out += "*".repeat(utf8Length(ch));
  return out;
}

function utf8Length(ch: string): number {
  const cp = ch.codePointAt(0) ?? 0;
  if (cp < 0x80) return 1;
  if (cp < 0x800) return 2;
  return cp < 0x10000 ? 3 : 4;
}

function normalizeName(name: string): string {
  return name.toLowerCase().replace(/[-_.\s]/g, "");
}

function isSecretName(name: string): boolean {
  const n = normalizeName(name);
  return (
    SECRET_NAMES.has(n) || n.endsWith("key") || SECRET_NAME_PARTS.some((p) => n.includes(p))
  );
}

// URL parameters and form fields: also any name ending in "code" (an OAuth
// code, auth_code, device_code). JSON bodies use "code" for error codes, so
// JSON keys don't get this rule. A bracketed name, as qs, Rails and PHP forms
// produce (user[otp_code], access_key[]), is also judged by the text in its
// last brackets, after dropping trailing "[]".
function isSecretParam(name: string): boolean {
  const base = name.replace(/(\[\])+$/, "");
  const last = /\[([^[\]]*)\]$/.exec(base)?.[1];
  return isSecretParamName(base) || (last !== undefined && isSecretParamName(last));
}

function isSecretParamName(name: string): boolean {
  return isSecretName(name) || normalizeName(name).endsWith("code");
}

function isSecretJsonKey(name: string): boolean {
  const n = normalizeName(name);
  return isSecretName(name) || n.includes("authorization") || n.includes("cookie");
}

function isSecretHeader(lowerName: string): boolean {
  if (NEVER_SECRET_HEADER.test(lowerName)) return false;
  return SECRET_HEADERS.has(lowerName) || isSecretName(lowerName);
}

function redactHeaders(headers: unknown): void {
  if (!Array.isArray(headers)) return;
  for (const header of headers) {
    if (!isRecord(header) || typeof header.name !== "string") continue;
    if (header.value === undefined || header.value === null) continue;
    const value = String(header.value);
    const masked = redactHeaderValue(header.name.toLowerCase(), value);
    if (masked !== value) header.value = masked;
  }
}

function redactHeaderValue(lowerName: string, value: string): string {
  switch (lowerName) {
    case "authorization":
      return redactBearerJwt(value) ?? redactCredentials(value);
    case "proxy-authorization":
      return redactCredentials(value);
    case "cookie":
      return redactCookieHeader(value);
    case "set-cookie":
    case "set-cookie2":
      return redactSetCookie(value);
  }
  if (URL_HEADERS.has(lowerName)) return maskUrl(value);
  if (isSecretHeader(lowerName)) return mask(value);
  return value;
}

// "<scheme> <credential>": keeps a known scheme and masks the credential.
function redactCredentials(value: string): string {
  const m = FIRST_WORD.exec(value);
  if (!m || !AUTH_SCHEMES.has((m[2] ?? "").toLowerCase())) return mask(value);
  return (m[1] ?? "") + (m[2] ?? "") + mask(m[3] ?? "");
}

// A Bearer JWT keeps its header part byte for byte, its claims part
// becomes a fresh encoding of only exp, nbf and sub, and each run of
// non-whitespace in its signature becomes the marker. Whitespace anywhere in
// the value is kept. Returns null for anything that isn't a Bearer JWT.
function redactBearerJwt(value: string): string | null {
  const m = BEARER.exec(value);
  if (!m) return null;
  const [, lead = "", scheme = "", gap = "", token = "", trail = ""] = m;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [first = "", claimsPart = "", signature = ""] = parts;
  const claims = decodeJwtPart(claimsPart);
  if (!claims) return null;
  // The first part is kept only if it is a JWT header. Anything else, such
  // as another credential in front of the JWT when the header was set twice
  // and the values were joined with ", ", is masked. Python never reads it.
  const header = JWT_HEADER_PART.test(first) && decodeJwtPart(first) ? first : mask(first);
  const claimsWhitespace = (claimsPart.match(WS_CHARS) ?? []).join("");
  return (
    lead +
    scheme +
    gap +
    header +
    "." +
    encodeKeptClaims(claims) +
    claimsWhitespace +
    "." +
    signature.replace(NON_WS_RUN, JWT_SIGNATURE_MARKER) +
    trail
  );
}

// Decodes a JWT part to a JSON object: drops characters outside base64url,
// re-pads and parses. On ordinary tokens this agrees with auth.py's
// _decode_jwt_payload. It doesn't copy Python's padding quirks, so on rare
// malformed tokens the two disagree either way: a claims part Python
// rejects (one with an inner space, say) can decode here, and the masked
// copy may then fire a JWT finding the original didn't; one Python accepts
// (a stray "=" after 4k+3 characters) can fail here, and the whole token is
// masked. Neither case leaks the credential.
function decodeJwtPart(part: string): Json | null {
  const b64 = part
    .replace(/[^A-Za-z0-9\-_+/]/g, "")
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  if (b64.length % 4 === 1) return null;
  let parsed: unknown;
  try {
    const binary = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
  return isRecord(parsed) ? parsed : null;
}

function encodeKeptClaims(claims: Json): string {
  const kept: Json = {};
  for (const [name, value] of Object.entries(claims)) {
    if (KEPT_CLAIMS.has(name)) kept[name] = value;
  }
  // Pad the JSON with spaces to a multiple of 3 bytes so its base64url needs
  // no "=" padding; Python then decodes it whatever whitespace the original
  // claims part carried (kept just after it).
  let json = JSON.stringify(kept);
  while (new TextEncoder().encode(json).length % 3 !== 0) json += " ";
  let binary = "";
  for (const byte of new TextEncoder().encode(json)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// "a=1; b=2": keeps names and separators, masks values. A pair without
// "=" is masked whole.
function redactCookieHeader(value: string): string {
  return value
    .split(";")
    .map((pair) => {
      const eq = pair.indexOf("=");
      return eq < 0 ? mask(pair) : pair.slice(0, eq + 1) + mask(pair.slice(eq + 1));
    })
    .join(";");
}

// One cookie per line (Chrome can join several Set-Cookie headers with
// "\n"): masks the value, keeps the name and attributes.
function redactSetCookie(value: string): string {
  return value
    .split("\n")
    .map((line) => {
      const semi = line.indexOf(";");
      const first = semi < 0 ? line : line.slice(0, semi);
      const attributes = semi < 0 ? "" : line.slice(semi);
      const eq = first.indexOf("=");
      const masked =
        eq < 0 ? mask(first) : first.slice(0, eq + 1) + mask(first.slice(eq + 1));
      return masked + attributes;
    })
    .join("\n");
}

function redactCookies(cookies: unknown): void {
  if (!Array.isArray(cookies)) return;
  for (const cookie of cookies) {
    if (isRecord(cookie) && cookie.value !== undefined && cookie.value !== null) {
      cookie.value = mask(String(cookie.value));
    }
  }
}

// HAR queryString and postData.params: [{name, value}, ...]. Names are
// decoded first, as in maskParams.
function redactNamedValues(list: unknown): void {
  if (!Array.isArray(list)) return;
  for (const item of list) {
    if (
      isRecord(item) &&
      typeof item.name === "string" &&
      item.value !== undefined &&
      item.value !== null &&
      isSecretParam(decodeParamName(item.name))
    ) {
      item.value = mask(String(item.value));
    }
  }
}

// Masks secret query and fragment parameter values and a password (or a
// bare token) in the userinfo. Works on the raw string, so everything else
// stays byte for byte, percent-encoding included.
function maskUrl(url: string): string {
  let prefix = "";
  let rest = url;
  const authority = /^([a-z][a-z0-9+.-]*:\/\/)([^/?#]*)/i.exec(url);
  if (authority) {
    const [whole, scheme = "", hostPart = ""] = authority;
    const at = hostPart.lastIndexOf("@");
    let maskedHost = hostPart;
    if (at >= 0) {
      const userinfo = hostPart.slice(0, at);
      const colon = userinfo.indexOf(":");
      maskedHost =
        (colon < 0
          ? mask(userinfo)
          : userinfo.slice(0, colon + 1) + mask(userinfo.slice(colon + 1))) +
        hostPart.slice(at);
    }
    prefix = scheme + maskedHost;
    rest = url.slice(whole.length);
  }
  const hash = rest.indexOf("#");
  const beforeHash = hash < 0 ? rest : rest.slice(0, hash);
  const q = beforeHash.indexOf("?");
  let out = prefix + (q < 0 ? beforeHash : beforeHash.slice(0, q));
  if (q >= 0) out += "?" + maskParams(beforeHash.slice(q + 1), mask);
  if (hash >= 0) out += "#" + maskParams(rest.slice(hash + 1), mask);
  return out;
}

function maskParams(text: string, maskValue: (value: string) => string): string {
  return text
    .split("&")
    .map((pair) => {
      const eq = pair.indexOf("=");
      if (eq < 0) return pair;
      const name = pair.slice(0, eq);
      return isSecretParam(decodeParamName(name))
        ? name + "=" + maskValue(pair.slice(eq + 1))
        : pair;
    })
    .join("&");
}

function decodeParamName(name: string): string {
  try {
    return decodeURIComponent(name.replace(/\+/g, " "));
  } catch {
    return name;
  }
}

// Bodies JSON.parse accepts get their secret string values masked in place;
// URL-encoded forms get their secret field values masked. Anything else
// (malformed JSON, XML, multipart, text) passes through unchanged.
function maskBody(text: string, mimeType: unknown): string {
  if (isJson(text)) return maskJsonStrings(text);
  if (
    typeof mimeType === "string" &&
    mimeType.toLowerCase().includes("application/x-www-form-urlencoded")
  ) {
    return maskParams(text, maskBytes);
  }
  return text;
}

function isJson(text: string): boolean {
  try {
    JSON.parse(text);
    return true;
  } catch {
    return false;
  }
}

// Masks, inside the raw text, every string value that sits (at any depth)
// under a secret-named key. Only the characters between a string's quotes
// change, one "*" per UTF-8 byte (escapes included), so the body keeps its
// formatting, validity and byte length. Never re-serializes: that would move
// whitespace and escapes, and with them body.content_length_mismatch and the
// Report's body preview. Numbers and booleans under secret keys are kept.
// Call only with text JSON.parse accepts.
function maskJsonStrings(text: string): string {
  const out: string[] = [];
  let copied = 0;
  let i = 0;

  const skipWhitespace = () => {
    while (i < text.length && " \t\n\r".includes(text[i] ?? "")) i++;
  };
  // At an opening quote: moves past the closing quote, returns the start.
  const readString = (): number => {
    const start = i++;
    while (text[i] !== '"') i += text[i] === "\\" ? 2 : 1;
    i++;
    return start;
  };
  const readValue = (secret: boolean): void => {
    skipWhitespace();
    const c = text[i];
    if (c === '"') {
      const start = readString();
      if (secret) {
        out.push(text.slice(copied, start + 1), maskBytes(text.slice(start + 1, i - 1)));
        copied = i - 1;
      }
    } else if (c === "{") {
      i++;
      skipWhitespace();
      while (text[i] !== "}") {
        const keyStart = readString();
        const key = JSON.parse(text.slice(keyStart, i)) as string;
        skipWhitespace();
        i++; // ":"
        readValue(secret || isSecretJsonKey(key));
        skipWhitespace();
        if (text[i] === ",") {
          i++;
          skipWhitespace();
        }
      }
      i++;
    } else if (c === "[") {
      i++;
      skipWhitespace();
      while (text[i] !== "]") {
        readValue(secret);
        skipWhitespace();
        if (text[i] === ",") i++;
      }
      i++;
    } else {
      while (i < text.length && !",}] \t\n\r".includes(text[i] ?? "")) i++;
    }
  };

  readValue(false);
  out.push(text.slice(copied));
  return out.join("");
}
