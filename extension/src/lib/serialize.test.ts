import { describe, it, expect } from "vitest";
import { buildAnalyzePayload, ANALYZER_CREATOR } from "./serialize";

describe("buildAnalyzePayload", () => {
  it("wraps a single entry in a minimal HAR log envelope", () => {
    const entry = {
      request: { method: "GET", url: "https://example.com/" },
      response: { status: 200 },
    };
    const payload = buildAnalyzePayload(entry);
    expect(payload.kind).toBe("har");
    expect(payload.har.log.version).toBe("1.2");
    expect(payload.har.log.creator).toEqual(ANALYZER_CREATOR);
    expect(payload.har.log.entries).toHaveLength(1);
    expect(payload.har.log.entries[0]).toBe(entry);
  });

  it("preserves the captured entry's auxiliary HAR fields", () => {
    const entry = {
      request: { method: "POST", url: "https://x" },
      response: { status: 401 },
      startedDateTime: "2026-04-29T15:00:00Z",
      time: 123,
      timings: { send: 1, wait: 100, receive: 22 },
    };
    const payload = buildAnalyzePayload(entry);
    const out = payload.har.log.entries[0];
    expect(out).toMatchObject({
      startedDateTime: "2026-04-29T15:00:00Z",
      time: 123,
      timings: { send: 1, wait: 100, receive: 22 },
    });
  });
});
