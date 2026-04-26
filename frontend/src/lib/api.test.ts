import { afterEach, describe, it, expect, vi } from "vitest";
import { runRequest, analyzeHar } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

const SAMPLE_REPORT = {
  source: "live",
  request: { method: "GET", url: "https://x.com/", headers: {}, body_size_bytes: 0 },
  timing: {},
  findings: [],
};

describe("runRequest", () => {
  it("posts JSON to /api/run and returns the parsed Report", async () => {
    let seenInit: RequestInit | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      expect(url).toMatch(/\/api\/run$/);
      seenInit = init;
      return new Response(JSON.stringify(SAMPLE_REPORT), { status: 200 });
    });

    const report = await runRequest({
      method: "GET",
      url: "https://x.com/",
      headers: { Accept: "*/*" },
    });

    expect(report.source).toBe("live");
    expect(seenInit?.method).toBe("POST");
    const body = JSON.parse(String(seenInit?.body));
    expect(body.method).toBe("GET");
    expect(body.headers).toEqual({ Accept: "*/*" });
  });

  it("throws with detail on non-2xx", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      return new Response(JSON.stringify({ detail: "boom" }), { status: 500 });
    });
    await expect(runRequest({ method: "GET", url: "https://x.com/" })).rejects.toThrow(
      /Run failed.*boom/,
    );
  });
});

describe("analyzeHar", () => {
  const VALID_HAR = {
    log: { version: "1.2", entries: [{ request: { method: "GET", url: "https://x.com/" } }] },
  };

  it("posts {kind: 'har', har} to /api/analyze and returns the Report", async () => {
    let seenBody: { kind?: string; har?: unknown } = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      expect(url).toMatch(/\/api\/analyze$/);
      seenBody = JSON.parse(String(init?.body ?? "{}"));
      return new Response(JSON.stringify(SAMPLE_REPORT), { status: 200 });
    });

    const file = new File([JSON.stringify(VALID_HAR)], "session.har");
    await analyzeHar(file);

    expect(seenBody.kind).toBe("har");
    expect(seenBody.har).toEqual(VALID_HAR);
  });

  it("throws when the file isn't valid JSON", async () => {
    const file = new File(["not json"], "bad.har");
    await expect(analyzeHar(file)).rejects.toThrow(/not valid JSON/);
  });

  it("throws with detail on API error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      return new Response(JSON.stringify({ detail: "bad har" }), { status: 400 });
    });
    const file = new File([JSON.stringify({ log: { entries: [] } })], "session.har");
    await expect(analyzeHar(file)).rejects.toThrow(/Analyze failed.*bad har/);
  });
});
