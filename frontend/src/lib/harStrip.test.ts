import { describe, it, expect } from "vitest";
import {
  HarTooLargeError,
  encodedPayloadSize,
  prepareHarForUpload,
  type HarFile,
} from "./harStrip";

function makeHar(entries: unknown[]): HarFile {
  return {
    log: {
      version: "1.2",
      creator: { name: "test", version: "0" },
      entries: entries as HarFile["log"]["entries"],
    },
  };
}

function entry(opts: { postBody?: string; respBody?: string; url?: string } = {}) {
  return {
    request: {
      method: "POST",
      url: opts.url ?? "https://api.example.com/v1/users",
      headers: [],
      postData: opts.postBody ? { mimeType: "text/plain", text: opts.postBody } : null,
    },
    response: {
      status: 200,
      statusText: "OK",
      headers: [],
      content: opts.respBody
        ? { size: opts.respBody.length, mimeType: "text/plain", text: opts.respBody }
        : null,
    },
  };
}

describe("prepareHarForUpload", () => {
  it("passthrough: small HAR is unchanged", () => {
    const har = makeHar([entry({ postBody: "hi", respBody: "ok" })]);
    const before = JSON.parse(JSON.stringify(har));

    const { har: out, summary } = prepareHarForUpload(har);

    expect(summary.action).toBe("passthrough");
    expect(summary.originalEntryCount).toBe(1);
    expect(summary.finalEntryCount).toBe(1);
    expect(summary.bodiesStripped).toBe(0);
    expect(out).toEqual(before);
  });

  it("strips bodies when over threshold but entries fit", () => {
    const big = "x".repeat(2000);
    const har = makeHar([
      entry({ postBody: big, respBody: big }),
      entry({ postBody: big, respBody: big }),
      entry({ postBody: big, respBody: big }),
    ]);
    const threshold = 5000;

    const { har: out, summary } = prepareHarForUpload(har, threshold);

    expect(summary.action).toBe("stripped-bodies");
    expect(summary.originalEntryCount).toBe(3);
    expect(summary.finalEntryCount).toBe(3);
    expect(summary.bodiesStripped).toBe(6);
    for (const e of out.log.entries) {
      expect(e.request?.postData?.text).toBe("");
      expect(e.response?.content?.text).toBe("");
    }
    expect(summary.finalBytes).toBeLessThanOrEqual(threshold);
  });

  it("falls back to first-entry only when stripping bodies isn't enough", () => {
    const filler = "u".repeat(400);
    const longUrl = `https://api.example.com/v1/things?q=${filler}`;
    const entries = Array.from({ length: 20 }, () => entry({ url: longUrl }));
    const har = makeHar(entries);
    const threshold = 1500;

    const { har: out, summary } = prepareHarForUpload(har, threshold);

    expect(summary.action).toBe("first-entry-only");
    expect(summary.originalEntryCount).toBe(20);
    expect(summary.finalEntryCount).toBe(1);
    expect(out.log.entries.length).toBe(1);
    expect(summary.finalBytes).toBeLessThanOrEqual(threshold);
  });

  it("throws HarTooLargeError when even the first entry exceeds the threshold", () => {
    // Single entry with an oversized URL that survives body-stripping and
    // first-entry slicing (the only data left after both tiers).
    const giantUrl = `https://api.example.com/?q=${"z".repeat(50_000)}`;
    const har = makeHar([entry({ url: giantUrl })]);
    const threshold = 1000;

    expect(() => prepareHarForUpload(har, threshold)).toThrow(HarTooLargeError);
  });

  it("threshold check is on the encoded {kind,har} payload, not raw HAR", () => {
    const har = makeHar([entry()]);
    const size = encodedPayloadSize(har);

    const { summary: under } = prepareHarForUpload(makeHar([entry()]), size + 100);
    expect(under.action).toBe("passthrough");

    const { summary: at } = prepareHarForUpload(makeHar([entry()]), size);
    expect(at.action).toBe("passthrough");
  });

  it("idempotent: re-running on a stripped HAR is a passthrough", () => {
    const big = "x".repeat(2000);
    const har = makeHar([entry({ postBody: big, respBody: big })]);
    const threshold = 1500;

    const first = prepareHarForUpload(har, threshold);
    expect(first.summary.action).toBe("stripped-bodies");

    const second = prepareHarForUpload(first.har, threshold);
    expect(second.summary.action).toBe("passthrough");
    expect(second.summary.bodiesStripped).toBe(0);
  });

  it("preserves request/response shape so the parser still gets headers and metadata", () => {
    const big = "x".repeat(2000);
    const har = makeHar([entry({ postBody: big, respBody: big })]);

    const { har: out } = prepareHarForUpload(har, 1500);

    const e = out.log.entries[0];
    expect(e).toBeDefined();
    expect(e?.request?.method).toBe("POST");
    expect(e?.request?.url).toBe("https://api.example.com/v1/users");
    expect(e?.response?.status).toBe(200);
    expect(e?.request?.postData).not.toBeNull();
    expect(e?.response?.content).not.toBeNull();
  });
});
