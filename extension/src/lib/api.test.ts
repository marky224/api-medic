import { describe, it, expect, vi, beforeEach } from "vitest";
import { analyzeHarEntry } from "./api";

const mockFetch = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch);
  mockFetch.mockReset();
});

describe("analyzeHarEntry", () => {
  it("POSTs to the hosted /api/analyze with a single-entry HAR payload", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ source: "extension" }),
    });
    const entry = {
      request: { method: "GET", url: "https://example.com/" },
      response: { status: 200 },
    };

    await analyzeHarEntry(entry);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const call = mockFetch.mock.calls[0];
    expect(call).toBeDefined();
    const [url, init] = call as [string, RequestInit];
    expect(url).toBe("https://api-medic.markandrewmarquez.com/api/analyze");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });

    const body = JSON.parse(init.body as string);
    expect(body.kind).toBe("har");
    expect(body.har.log.version).toBe("1.2");
    expect(body.har.log.entries).toHaveLength(1);
    expect(body.har.log.entries[0]).toEqual(entry);
  });

  it("returns the parsed Report on success", async () => {
    const report = { source: "extension", id: "abc" };
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => report,
    });

    const result = await analyzeHarEntry({});

    expect(result).toEqual(report);
  });

  it("throws with the server's detail message when error body is JSON", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      json: async () => ({ detail: "URL is not allowed" }),
    });

    await expect(analyzeHarEntry({})).rejects.toThrow(
      "Analyze failed: URL is not allowed",
    );
  });

  it("falls back to status text when error body is not JSON", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new Error("not json");
      },
    });

    await expect(analyzeHarEntry({})).rejects.toThrow(
      "Analyze failed: 502 Bad Gateway",
    );
  });

  it("falls back to status text when error JSON has no detail field", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => ({ error: "something" }),
    });

    await expect(analyzeHarEntry({})).rejects.toThrow(
      "Analyze failed: 500 Internal Server Error",
    );
  });
});
