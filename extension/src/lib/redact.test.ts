import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { JWT_SIGNATURE_MARKER, redactEntry } from "./redact";
import {
  REDACTION_FIXTURES_DIR,
  loadRedactionScenarios,
} from "../test/redaction-fixtures";

type Header = { name: string; value: string };
type Entry = {
  request: {
    url: string;
    headers: Header[];
    queryString?: { name: string; value: string }[];
    cookies?: Record<string, unknown>[];
    postData?: { mimeType: string; text: string; params?: { name: string; value: string }[] };
  };
  response: {
    status: number;
    headers: Header[];
    cookies?: Record<string, unknown>[];
    content?: { mimeType: string; text?: string; encoding?: string };
    redirectURL?: string;
  };
  [k: string]: unknown;
};

function entry(overrides: {
  url?: string;
  headers?: Header[];
  responseHeaders?: Header[];
  request?: Partial<Entry["request"]>;
  response?: Partial<Entry["response"]>;
} = {}): Entry {
  return {
    request: {
      url: overrides.url ?? "https://api.example.com/v1/me",
      headers: overrides.headers ?? [],
      ...overrides.request,
    },
    response: {
      status: 200,
      headers: overrides.responseHeaders ?? [],
      ...overrides.response,
    },
  };
}

function redact(e: Entry): Entry {
  return redactEntry(e) as Entry;
}

function header(e: Entry, name: string, side: "request" | "response" = "request"): string {
  const found = e[side].headers.find((h) => h.name === name);
  if (!found) throw new Error(`no ${name} header`);
  return found.value;
}

function maskedAuthorization(value: string): string {
  return header(redact(entry({ headers: [{ name: "Authorization", value }] })), "Authorization");
}

const b64url = (text: string) => Buffer.from(text, "utf8").toString("base64url");
const decodeB64url = (part: string) =>
  JSON.parse(Buffer.from(part.replace(/\s/g, ""), "base64url").toString("utf8")) as unknown;

const JWT_HEADER = b64url(JSON.stringify({ alg: "HS256", typ: "JWT", kid: "k1" }));
const CLAIMS = {
  email: "dana@corp.test",
  sub: "user-42",
  exp: 1600000000,
  nbf: 1599990000.5,
  roles: ["admin"],
};
const JWT = `${JWT_HEADER}.${b64url(JSON.stringify(CLAIMS))}.c2lnbmF0dXJlLWJ5dGVz`;

const utf8Bytes = (s: string) => new TextEncoder().encode(s).length;

describe("redactEntry: Authorization", () => {
  it("keeps a Bearer JWT's header part, only exp/nbf/sub, and replaces the signature", () => {
    const out = maskedAuthorization(`Bearer ${JWT}`);
    const [scheme, token] = out.split(" ");
    expect(scheme).toBe("Bearer");
    const [headerPart, claimsPart, signature] = token!.split(".");
    expect(headerPart).toBe(JWT_HEADER);
    expect(signature).toBe(JWT_SIGNATURE_MARKER);
    // Unpadded base64url that needs no padding at all.
    expect(claimsPart).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(claimsPart!.length % 4).toBe(0);
    // Same claims, same JSON types, in the original order; nothing else.
    expect(Object.keys(decodeB64url(claimsPart!) as object)).toEqual(["sub", "exp", "nbf"]);
    expect(decodeB64url(claimsPart!)).toEqual({
      sub: "user-42",
      exp: 1600000000,
      nbf: 1599990000.5,
    });
    expect(out).not.toContain("c2lnbmF0dXJlLWJ5dGVz");
  });

  it("keeps whitespace at the edges and inside the claims part and signature", () => {
    const claimsPart = b64url(JSON.stringify(CLAIMS));
    const value = `\u0085bearer\t ${JWT_HEADER}.${claimsPart.slice(0, 7)}\r\n${claimsPart.slice(7)}.c2ln\nbmF0 `;
    const out = maskedAuthorization(value);
    expect(out.startsWith(`\u0085bearer\t ${JWT_HEADER}.`)).toBe(true);
    expect(out.endsWith(`.${JWT_SIGNATURE_MARKER}\n${JWT_SIGNATURE_MARKER} `)).toBe(true);
    const claims = out.split(".")[1]!;
    expect(claims.endsWith("\r\n")).toBe(true);
    expect(decodeB64url(claims)).toEqual({ sub: "user-42", exp: 1600000000, nbf: 1599990000.5 });
  });

  it("keeps an empty claims object when exp, nbf and sub are absent", () => {
    const token = `${JWT_HEADER}.${b64url('{"iss":"me"}')}.sig`;
    const claims = maskedAuthorization(`Bearer ${token}`).split(".")[1]!;
    expect(decodeB64url(claims)).toEqual({});
  });

  it("masks an opaque Bearer token and keeps the scheme and length", () => {
    expect(maskedAuthorization("Bearer abc-123.xyz")).toBe("Bearer ***********");
  });

  it("masks a three-part token whose middle part isn't JSON", () => {
    expect(maskedAuthorization("Bearer ya29.c.Kp0BsecretPart")).toBe(
      `Bearer ${"*".repeat("ya29.c.Kp0BsecretPart".length)}`,
    );
  });

  it("masks an opaque credential joined in front of a JWT (a header set twice)", () => {
    // fetch's Headers.append and XHR's setRequestHeader join values with ", ".
    const out = maskedAuthorization(`Bearer opaque-Secret_123, Bearer ${JWT}`);
    expect(out).not.toContain("opaque-Secret_123");
    expect(out.startsWith("Bearer ")).toBe(true);
    const [firstPart, claimsPart, signature] = out.slice("Bearer ".length).split(".");
    expect(firstPart).toMatch(/^[* ]+$/);
    expect(decodeB64url(claimsPart!)).toEqual({ sub: "user-42", exp: 1600000000, nbf: 1599990000.5 });
    expect(signature).toBe(JWT_SIGNATURE_MARKER);
  });

  it("masks a first part that isn't a JWT header", () => {
    const out = maskedAuthorization(`Bearer PREFIXSECRET.${b64url('{"exp":1}')}.x`);
    expect(out.startsWith("Bearer ************.")).toBe(true);
    expect(out).not.toContain("PREFIXSECRET");
    expect(decodeB64url(out.split(".")[1]!)).toEqual({ exp: 1 });
  });

  it("masks a first part that isn't pure base64url, even if it decodes", () => {
    // The lenient decoder drops "!@#$" and would accept what is left.
    const claims = b64url(JSON.stringify(CLAIMS));
    const out = maskedAuthorization(`Bearer ${JWT_HEADER}!@#$.${claims}.x`);
    const firstPart = out.slice("Bearer ".length).split(".")[0]!;
    expect(firstPart).toBe("*".repeat(JWT_HEADER.length + 4));
  });

  it("treats BEARER like any other scheme, because the auth checks don't read it", () => {
    const out = maskedAuthorization(`BEARER ${JWT}`);
    expect(out).toBe(`BEARER ${"*".repeat(JWT.length)}`);
  });

  it("keeps Basic and other known schemes, masks the credential", () => {
    expect(maskedAuthorization("Basic dXNlcjpwYXNz")).toBe("Basic ************");
    expect(maskedAuthorization('Digest username="a", response="b"')).toBe(
      `Digest ${"*".repeat('username="a",'.length)} ${"*".repeat('response="b"'.length)}`,
    );
  });

  it("masks the whole value when the first word isn't a known scheme", () => {
    expect(maskedAuthorization("sk_live_abc123")).toBe("**************");
    expect(maskedAuthorization("Custom abc def")).toBe("****** *** ***");
  });

  it("keeps whitespace that only Python counts, at both edges", () => {
    expect(maskedAuthorization("\u001fBearer tok\u0085")).toBe("\u001fBearer ***\u0085");
  });

  it("masks every Authorization header, whatever its case", () => {
    const out = redact(
      entry({
        headers: [
          { name: "authorization", value: "Bearer one" },
          { name: "AUTHORIZATION", value: "Bearer two" },
        ],
      }),
    );
    expect(out.request.headers.map((h) => h.value)).toEqual(["Bearer ***", "Bearer ***"]);
  });

  it("never adds or removes an Authorization header", () => {
    const without = redact(entry({ headers: [{ name: "Accept", value: "*/*" }] }));
    expect(without.request.headers.map((h) => h.name)).toEqual(["Accept"]);
    const empty = redact(entry({ headers: [{ name: "Authorization", value: "" }] }));
    expect(header(empty, "Authorization")).toBe("");
  });

  it("masks Proxy-Authorization without keeping JWT claims", () => {
    const out = redact(entry({ headers: [{ name: "Proxy-Authorization", value: `Bearer ${JWT}` }] }));
    expect(header(out, "Proxy-Authorization")).toBe(`Bearer ${"*".repeat(JWT.length)}`);
  });
});

describe("redactEntry: cookies", () => {
  it("keeps cookie names and separators, masks values", () => {
    const out = redact(
      entry({ headers: [{ name: "Cookie", value: "a=1; b=x=y;  flag; c=" }] }),
    );
    expect(header(out, "Cookie")).toBe("a=*; b=***;  ****; c=");
  });

  it("masks each Set-Cookie value and keeps the attributes", () => {
    const out = redact(
      entry({
        responseHeaders: [
          {
            name: "set-cookie",
            value: "sid=abc123; Path=/; HttpOnly; SameSite=Lax\nother=xy; Max-Age=0",
          },
        ],
      }),
    );
    expect(header(out, "set-cookie", "response")).toBe(
      "sid=******; Path=/; HttpOnly; SameSite=Lax\nother=**; Max-Age=0",
    );
  });

  it("masks values in the HAR cookie lists and keeps the other fields", () => {
    const out = redact(
      entry({
        request: { cookies: [{ name: "sid", value: "abc", httpOnly: true }] },
        response: { cookies: [{ name: "r", value: "xyz", path: "/" }] },
      }),
    );
    expect(out.request.cookies).toEqual([{ name: "sid", value: "***", httpOnly: true }]);
    expect(out.response.cookies).toEqual([{ name: "r", value: "***", path: "/" }]);
  });
});

describe("redactEntry: other headers", () => {
  it("masks secret-named headers on both sides", () => {
    const names = [
      "X-Api-Key",
      "x-auth-token",
      "X-CSRF-Token",
      "X-Amz-Security-Token",
      "Private-Token",
      "Ocp-Apim-Subscription-Key",
      "X-Functions-Key",
      "DPoP",
      "X-Hub-Signature-256",
      "X-Session-Id",
      "X-Client-Secret",
    ];
    const headers = names.map((name) => ({ name, value: "s3cret value" }));
    const out = redact(entry({ headers, responseHeaders: headers }));
    for (const side of ["request", "response"] as const) {
      for (const h of out[side].headers) expect(h.value, h.name).toBe("****** *****");
    }
  });

  it("masks a header named exactly Code, but not other header names ending in code", () => {
    const out = redact(
      entry({
        headers: [
          { name: "Code", value: "s3cret" },
          { name: "X-Error-Code", value: "E42" },
        ],
        responseHeaders: [{ name: "code", value: "s3cret" }],
      }),
    );
    expect(out.request.headers).toEqual([
      { name: "Code", value: "******" },
      { name: "X-Error-Code", value: "E42" },
    ]);
    expect(header(out, "code", "response")).toBe("******");
  });

  it("masks non-string values of secret headers and cookies, and keeps other non-strings", () => {
    const out = redactEntry({
      request: {
        url: "https://api.example.com/",
        headers: [
          { name: "X-Api-Key", value: 12345 },
          { name: "Authorization", value: 67890 },
          { name: "Content-Length", value: 12 },
        ],
        cookies: [{ name: "n", value: 42 }],
      },
      response: { status: 200, headers: [] },
    }) as { request: { headers: unknown[]; cookies: unknown[] } };
    expect(out.request.headers).toEqual([
      { name: "X-Api-Key", value: "*****" },
      { name: "Authorization", value: "*****" },
      { name: "Content-Length", value: 12 },
    ]);
    expect(out.request.cookies).toEqual([{ name: "n", value: "**" }]);
  });

  it("leaves every header a check reads, and other non-secret headers, byte for byte", () => {
    const checkRead = JSON.parse(
      fs.readFileSync(path.join(REDACTION_FIXTURES_DIR, "check-read-headers.json"), "utf8"),
    ) as string[];
    // Authorization is masked by design; the parity fixtures prove its
    // findings survive.
    const names = checkRead
      .filter((n) => n !== "Authorization")
      .concat([
        "WWW-Authenticate",
        "Proxy-Authenticate",
        "Access-Control-Allow-Credentials",
        "Access-Control-Allow-Headers",
        "Sec-WebSocket-Key",
        "Accept",
        "User-Agent",
        "Keep-Alive",
      ]);
    const headers = names.map((name) => ({
      name,
      value: name === "Location" ? "https://example.com/next?page=2" : "Bearer realm=\"x\", token-ish 123",
    }));
    const out = redact(entry({ headers, responseHeaders: headers }));
    expect(out.request.headers).toEqual(headers);
    expect(out.response.headers).toEqual(headers);
  });
});

describe("redactEntry: URLs", () => {
  it("masks secret query and fragment values, keeps everything else byte for byte", () => {
    const out = redact(
      entry({
        url: "https://api.example.com/v1/x?page=2&access_token=ab%2Fc&API%5FKEY=k1&q=a+b&code=xyz#id_token=t0k&tab=3",
      }),
    );
    expect(out.request.url).toBe(
      "https://api.example.com/v1/x?page=2&access_token=******&API%5FKEY=**&q=a+b&code=***#id_token=***&tab=3",
    );
  });

  it("masks a password in the userinfo, or the whole userinfo when it's a bare token", () => {
    expect(redact(entry({ url: "https://bob:hunter2@h.test/p" })).request.url).toBe(
      "https://bob:*******@h.test/p",
    );
    expect(redact(entry({ url: "https://ghp_abc123@github.com/o/r" })).request.url).toBe(
      "https://**********@github.com/o/r",
    );
  });

  it("masks query values in :path, Referer, Location, Content-Location and redirectURL", () => {
    const out = redact(
      entry({
        headers: [
          { name: ":path", value: "/cb?code=abc&state=s1" },
          { name: "Referer", value: "https://app.test/?token=t1" },
        ],
        responseHeaders: [
          { name: "Location", value: "https://app.test/home?sig=zz" },
          { name: "Content-Location", value: "/r?password=pw" },
        ],
        response: { redirectURL: "https://app.test/home?sig=zz" },
      }),
    );
    expect(header(out, ":path")).toBe("/cb?code=***&state=s1");
    expect(header(out, "Referer")).toBe("https://app.test/?token=**");
    expect(header(out, "Location", "response")).toBe("https://app.test/home?sig=**");
    expect(header(out, "Content-Location", "response")).toBe("/r?password=**");
    expect(out.response.redirectURL).toBe("https://app.test/home?sig=**");
  });

  it("decodes names in the queryString and postData.params lists before matching", () => {
    const out = redact(
      entry({
        request: {
          queryString: [
            { name: "to%6Ben", value: "t1" },
            { name: "api%5Fkey", value: "k1" },
          ],
          postData: {
            mimeType: "application/x-www-form-urlencoded",
            text: "pass%77ord=pw",
            params: [{ name: "pass%77ord", value: "pw" }],
          },
        },
      }),
    );
    expect(out.request.queryString).toEqual([
      { name: "to%6Ben", value: "**" },
      { name: "api%5Fkey", value: "**" },
    ]);
    expect(out.request.postData!.params).toEqual([{ name: "pass%77ord", value: "**" }]);
  });

  it("masks secret values in the HAR queryString list", () => {
    const out = redact(
      entry({
        request: {
          queryString: [
            { name: "api_key", value: "k1" },
            { name: "page", value: "2" },
          ],
        },
      }),
    );
    expect(out.request.queryString).toEqual([
      { name: "api_key", value: "**" },
      { name: "page", value: "2" },
    ]);
  });
});

describe("redactEntry: credential names", () => {
  // Names that end in "key" or "code", or name an assertion or verifier.
  const PARAM_NAMES = [
    "subscription-key",
    "access_key",
    "accessKey",
    "appKey",
    "client_assertion",
    "code_verifier",
    "auth_code",
    "device_code",
  ];

  it("masks them as URL parameters", () => {
    const query = PARAM_NAMES.map((n) => `${n}=v1`).join("&");
    const out = redact(entry({ url: `https://api.example.com/x?${query}&page=2` }));
    expect(out.request.url).toBe(
      `https://api.example.com/x?${PARAM_NAMES.map((n) => `${n}=**`).join("&")}&page=2`,
    );
  });

  it("masks them as form fields, in the body and the params list", () => {
    const out = redact(
      entry({
        request: {
          postData: {
            mimeType: "application/x-www-form-urlencoded",
            text: `${PARAM_NAMES.map((n) => `${n}=v1`).join("&")}&grant_type=x`,
            params: PARAM_NAMES.map((name) => ({ name, value: "v1" })),
          },
        },
      }),
    );
    expect(out.request.postData!.text).toBe(
      `${PARAM_NAMES.map((n) => `${n}=**`).join("&")}&grant_type=x`,
    );
    expect(out.request.postData!.params).toEqual(
      PARAM_NAMES.map((name) => ({ name, value: "**" })),
    );
  });

  // Bracketed and array-style names, as qs, Rails and PHP forms produce.
  const BRACKETED = [
    { name: "user[otp_code]", value: "123456", masked: "******" },
    { name: "auth[access_key]", value: "AKIAEXAMPLE", masked: "***********" },
    { name: "two_factor[code]", value: "654321", masked: "******" },
    { name: "user[password]", value: "pw", masked: "**" },
    { name: "access_key[]", value: "AK2", masked: "***" },
    { name: "user[email]", value: "a@b.test", masked: "a@b.test" },
    { name: "page[]", value: "2", masked: "2" },
  ];

  it("checks the last bracket segment of form field names", () => {
    const out = redact(
      entry({
        request: {
          postData: {
            mimeType: "application/x-www-form-urlencoded",
            text: BRACKETED.map((p) => `${p.name}=${p.value}`).join("&"),
            params: BRACKETED.map(({ name, value }) => ({ name, value })),
          },
        },
      }),
    );
    expect(out.request.postData!.text).toBe(
      BRACKETED.map((p) => `${p.name}=${p.masked}`).join("&"),
    );
    expect(out.request.postData!.params).toEqual(
      BRACKETED.map((p) => ({ name: p.name, value: p.masked })),
    );
  });

  it("checks the last bracket segment of query names, encoded or not", () => {
    const out = redact(
      entry({
        url: `https://api.example.com/x?${BRACKETED.map((p) => `${p.name}=${p.value}`).join("&")}&user%5Botp_code%5D=1`,
        request: { queryString: BRACKETED.map(({ name, value }) => ({ name, value })) },
      }),
    );
    expect(out.request.url).toBe(
      `https://api.example.com/x?${BRACKETED.map((p) => `${p.name}=${p.masked}`).join("&")}&user%5Botp_code%5D=*`,
    );
    expect(out.request.queryString).toEqual(
      BRACKETED.map((p) => ({ name: p.name, value: p.masked })),
    );
  });

  it("masks X-Access-Key, X-Auth-Key and X-Master-Key headers", () => {
    const headers = ["X-Access-Key", "X-Auth-Key", "X-Master-Key"].map((name) => ({
      name,
      value: "k1",
    }));
    const out = redact(entry({ headers }));
    expect(out.request.headers.map((h) => h.value)).toEqual(["**", "**", "**"]);
  });

  it("masks JSON keys ending in key, assertion or verifier, but keeps ones ending in code", () => {
    const text =
      '{"accessKey":"a1","appKey":"a2","client_assertion":"a3","code_verifier":"a4","error_code":"E1"}';
    const out = redact(entry({ response: { content: { mimeType: "application/json", text } } }));
    expect(out.response.content!.text).toBe(
      '{"accessKey":"**","appKey":"**","client_assertion":"**","code_verifier":"**","error_code":"E1"}',
    );
  });
});

describe("redactEntry: bodies", () => {
  const jsonBody = (text: string, mimeType = "application/json") =>
    redact(entry({ response: { content: { mimeType, text } } })).response.content!.text!;

  it("masks secret JSON strings in place, escapes and non-ASCII included, keeping the byte length", () => {
    // Raw body text: an escaped quote, a é escape and a raw é inside a
    // secret value, and a secret key nested inside an array.
    const text =
      '{\n  "access_token": "a\\"b\\u00e9céd",\n  "items": [{"password": "péw"}, 3],\n  "note": "keep"\n}';
    const out = jsonBody(text);
    expect(out).toBe(
      '{\n  "access_token": "' +
        "*".repeat(14) +
        '",\n  "items": [{"password": "' +
        "*".repeat(4) +
        '"}, 3],\n  "note": "keep"\n}',
    );
    expect(utf8Bytes(out)).toBe(utf8Bytes(text));
    expect(() => JSON.parse(out) as unknown).not.toThrow();
  });

  it("masks every string under a secret key at any depth, and leaves numbers alone", () => {
    const out = jsonBody('{"tokens":["aa",{"x":"bb"}],"secret":{"v":"cc","n":12},"pin":1234}');
    expect(out).toBe('{"tokens":["**",{"x":"**"}],"secret":{"v":"**","n":12},"pin":1234}');
  });

  it("keeps a JSON error code, which isn't a credential", () => {
    expect(jsonBody('{"error":{"code":"invalid_grant"}}')).toBe(
      '{"error":{"code":"invalid_grant"}}',
    );
  });

  it("passes malformed JSON, plain text and base64 bodies through unchanged", () => {
    expect(jsonBody('{"password": "pw", oops')).toBe('{"password": "pw", oops');
    expect(jsonBody("password=pw", "text/plain")).toBe("password=pw");
    const b64 = redact(
      entry({ response: { content: { mimeType: "application/json", text: "eyJ0b2tlbiI6IngifQ==", encoding: "base64" } } }),
    );
    expect(b64.response.content!.text).toBe("eyJ0b2tlbiI6IngifQ==");
  });

  it("masks secret fields of a URL-encoded form body and its params list", () => {
    const out = redact(
      entry({
        request: {
          postData: {
            mimeType: "application/x-www-form-urlencoded; charset=UTF-8",
            text: "grant_type=authorization_code&code=c%C3%A9d&client_secret=s1&user=al",
            params: [
              { name: "code", value: "céd" },
              { name: "user", value: "al" },
            ],
          },
        },
      }),
    );
    expect(out.request.postData!.text).toBe(
      "grant_type=authorization_code&code=********&client_secret=**&user=al",
    );
    expect(out.request.postData!.params).toEqual([
      { name: "code", value: "***" },
      { name: "user", value: "al" },
    ]);
  });
});

describe("redactEntry: the entry itself", () => {
  it("drops browser-specific fields at any depth", () => {
    const e = {
      ...entry(),
      _initiator: { type: "script", url: "https://app.test/a.js?token=x" },
      _webSocketMessages: [{ data: "secret" }],
      time: 5,
    };
    (e.request as Record<string, unknown>)._priority = "High";
    const out = redact(e as Entry);
    expect(out).not.toHaveProperty("_initiator");
    expect(out).not.toHaveProperty("_webSocketMessages");
    expect(out.request).not.toHaveProperty("_priority");
    expect(out.time).toBe(5);
  });

  it("returns a copy, leaves the input alone, and drops functions", () => {
    const e = { ...entry({ headers: [{ name: "Authorization", value: "Bearer abc" }] }), getContent: () => undefined };
    const out = redactEntry(e) as Record<string, unknown>;
    expect(out).not.toBe(e);
    expect(e.request.headers[0]!.value).toBe("Bearer abc");
    expect(out).not.toHaveProperty("getContent");
  });

  it("passes non-entry input through without throwing", () => {
    expect(redactEntry(null)).toBeNull();
    expect(redactEntry("x")).toBe("x");
    expect(redactEntry([1])).toEqual([1]);
    expect(redactEntry({})).toEqual({});
    expect(redactEntry({ request: "nope", response: { headers: "nope" } })).toEqual({
      request: "nope",
      response: { headers: "nope" },
    });
  });

  it.each(loadRedactionScenarios())("$name: masking twice changes nothing more", ({ fixture }) => {
    const once = redactEntry(fixture.entry);
    expect(redactEntry(once)).toEqual(once);
  });
});

// Writes tests/fixtures/redaction/<name>.redacted.json, which
// tests/unit/test_redaction_parity.py feeds to the Python analyzer. CI never
// writes snapshots, so a stale file fails there; refresh locally with
// `npx vitest run -u src/lib/redact.test.ts`.
describe("redactEntry: masked fixtures for the Python parity test", () => {
  const asciiJson = (value: unknown) =>
    JSON.stringify(value, null, 2).replace(
      /[\u007f-￿]/g,
      (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
    ) + "\n";

  it.each(loadRedactionScenarios())("$name", async ({ name, fixture }) => {
    await expect(asciiJson(redactEntry(fixture.entry))).toMatchFileSnapshot(
      path.join(REDACTION_FIXTURES_DIR, `${name}.redacted.json`),
    );
  });
});
