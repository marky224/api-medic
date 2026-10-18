import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildAnalyzePayload, ANALYZER_CREATOR } from "./serialize";
import {
  expectNoSecrets,
  loadRedactionScenario,
  loadRedactionScenarios,
} from "../test/redaction-fixtures";

const here = path.dirname(fileURLToPath(import.meta.url));

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
    // A masked copy: equal content (nothing to mask here), never the
    // captured object itself.
    expect(payload.har.log.entries[0]).toEqual(entry);
    expect(payload.har.log.entries[0]).not.toBe(entry);
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

  it.each(loadRedactionScenarios())(
    "$name: no secret reaches the serialized payload",
    ({ fixture }) => {
      const json = JSON.stringify(buildAnalyzePayload(fixture.entry));
      expectNoSecrets(json, fixture.secrets);
    },
  );

  it("leaves the captured entry unchanged", () => {
    const { entry } = loadRedactionScenario("90-no-leak");
    const before = JSON.parse(JSON.stringify(entry)) as unknown;
    buildAnalyzePayload(entry);
    expect(entry).toEqual(before);
  });

  it("reports the extension version shared by package.json and both manifests", () => {
    const versionOf = (file: string): string =>
      (
        JSON.parse(
          fs.readFileSync(path.resolve(here, "..", "..", file), "utf8"),
        ) as { version: string }
      ).version;
    const pkg = versionOf("package.json");
    expect(ANALYZER_CREATOR.version).toBe(pkg);
    expect(versionOf("public/manifest.json")).toBe(pkg);
    expect(versionOf("public/manifest.firefox.json")).toBe(pkg);
  });
});
