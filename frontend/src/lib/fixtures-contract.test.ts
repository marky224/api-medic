import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Report } from "./types";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = path.resolve(
  here,
  "..",
  "..",
  "..",
  "tests",
  "fixtures",
  "reports",
);

function readFixtures(): { filename: string; report: Report }[] {
  return fs
    .readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((filename) => {
      const raw = fs.readFileSync(path.join(FIXTURES_DIR, filename), "utf8");
      return { filename, report: JSON.parse(raw) as Report };
    });
}

describe("fixture contract", () => {
  const fixtures = readFixtures();

  it("ships the eight scenarios called out in architecture.md", () => {
    expect(fixtures).toHaveLength(8);
  });

  it.each(fixtures)("$filename has the required Report shape", ({ report }) => {
    expect(typeof report.source).toBe("string");
    expect(report.request).toBeDefined();
    expect(typeof report.request.method).toBe("string");
    expect(typeof report.request.url).toBe("string");
    expect(report.timing).toBeDefined();
    // findings is optional in the schema but every Phase 1 fixture should
    // include the field, even if empty — it's the surface the UI renders.
    expect(Array.isArray(report.findings)).toBe(true);
  });

  it("02-jwt-expired contains a JWT-expired finding (canonical scenario)", () => {
    const f = fixtures.find((x) => x.filename === "02-jwt-expired.json");
    expect(f).toBeDefined();
    const ids = f!.report.findings?.map((finding) => finding.id) ?? [];
    expect(ids).toContain("auth.jwt.expired");
  });
});
