import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { renderMarkdown } from "./markdown";
import type { Finding, Report } from "./types";

// Each Report beside the markdown both renderers write for it;
// tests/unit/test_renderers.py renders them with markdown.py and parses
// each .md as CommonMark.
const here = path.dirname(fileURLToPath(import.meta.url));
const SHARED_CASES = path.resolve(
  here,
  "..",
  "..",
  "..",
  "tests",
  "fixtures",
  "markdown",
);

function sharedCases(): string[] {
  return fs
    .readdirSync(SHARED_CASES)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.replace(/\.json$/, ""))
    .sort();
}

// UTF-8 on every OS: each .md holds an em dash and an arrow.
function read(name: string): string {
  return fs.readFileSync(path.join(SHARED_CASES, name), "utf8");
}

function reportWith(finding: Finding): Report {
  return {
    source: "har",
    request: { method: "GET", url: "https://api.example.com/", body_size_bytes: 0 },
    timing: {},
    findings: [finding],
  };
}

const IN_BACKTICKS = "`![t](https://evil.example/p.png)`";

describe("renderMarkdown escapes text from the Report", () => {
  const cases = sharedCases();

  it("has shared cases to render", () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  it.each(cases)("renders %s as markdown.py does", (name) => {
    const report = JSON.parse(read(`${name}.json`)) as Report;
    expect(renderMarkdown(report)).toBe(read(`${name}.md`));
  });

  it("puts JSON evidence holding backticks in a longer fence", () => {
    const duplicates = [{ name: "X-A", values: [`a${IN_BACKTICKS}`, "b"] }];
    const out = renderMarkdown(
      reportWith({
        id: "http.headers.duplicate",
        severity: "warning",
        title: "Header repeated with different values",
        explanation: "X-A is given twice.",
        evidence: { duplicates },
      }),
    );
    expect(out).toContain(`- \`duplicates\`: \`\`${JSON.stringify(duplicates)}\`\`\n`);
  });

  it("shows a title and an evidence key from the input as text", () => {
    // No check puts input in a title or an evidence key, so the Report is
    // built by hand: this fails if either stops being escaped.
    const out = renderMarkdown(
      reportWith({
        id: "http.example.title",
        severity: "warning",
        title: "x [a](https://evil.example/)",
        explanation: "An explanation.",
        evidence: { [`k${IN_BACKTICKS}`]: "v" },
      }),
    );
    expect(out).toContain("\n### [WARNING] x \\[a\\](https://evil.example/)\n");
    // The key ends with a backtick, so a space pads it inside the fence.
    expect(out).toContain(`\n- \`\` k${IN_BACKTICKS} \`\`: \`v\`\n`);
  });

  it("leaves *, _, ~ and backticks from the input as they are", () => {
    // Only what makes a link, an image, HTML, an entity or a line break
    // is escaped, so these still format the text they wrap.
    const origin = "https://o.example *x* _y_ ~z~ `w`";
    const explanation = `The request was made from origin ${origin}, but the response has no Access-Control-Allow-Origin header.`;
    const fix = `Set Access-Control-Allow-Origin: ${origin} on the server.`;
    const out = renderMarkdown(
      reportWith({
        id: "http.cors.misconfigured",
        severity: "critical",
        title: "CORS response missing Access-Control-Allow-Origin",
        explanation,
        evidence: { request_origin: origin },
        suggested_fix: fix,
      }),
    );
    expect(out).toContain(`\n${explanation}\n`);
    expect(out).toContain(`\n**Suggested fix:** ${fix}\n`);
  });

  it("renders long input quickly", () => {
    // The escaping runs regexes on the input, which can be long.
    const values = [
      "\\[".repeat(100_000),
      "&a".repeat(100_000),
      "&" + "a".repeat(200_000),
      "`a".repeat(100_000),
      "\n".repeat(200_000),
    ];
    for (const value of values) {
      const report = reportWith({
        id: "http.cors.misconfigured",
        severity: "critical",
        title: "CORS response missing Access-Control-Allow-Origin",
        explanation: `The request was made from origin ${value}.`,
        evidence: { request_origin: value },
        suggested_fix: `Set Access-Control-Allow-Origin: ${value}.`,
      });
      const started = performance.now();
      const out = renderMarkdown(report);
      const elapsed = performance.now() - started;
      expect(out.startsWith("# api-medic — diagnostic report\n")).toBe(true);
      expect(elapsed).toBeLessThan(1000);
    }
  });
});

describe("renderMarkdown's trailing whitespace", () => {
  it("is dropped quickly after a long run of spaces and tabs", () => {
    // A regex dropping the trailing whitespace rescanned every run of
    // spaces or tabs in the report, in time that grew with the square of
    // the run's length.
    const run = " \t".repeat(25_000);
    const report = reportWith({
      id: "http.cors.misconfigured",
      severity: "critical",
      title: "CORS response missing Access-Control-Allow-Origin",
      explanation: `The request was made from origin a${run}b.`,
      evidence: { request_origin: `a${run}b` },
      suggested_fix: `Set Access-Control-Allow-Origin: a${run}`,
    });
    const started = performance.now();
    const out = renderMarkdown(report);
    const elapsed = performance.now() - started;
    expect(out).toContain(`\nThe request was made from origin a${run}b.\n`);
    expect(out).toContain(`\n- \`request_origin\`: \`a${run}b\`\n`);
    // The run that ends the report goes, as it did.
    expect(out.endsWith("\n**Suggested fix:** Set Access-Control-Allow-Origin: a\n")).toBe(true);
    expect(elapsed).toBeLessThan(1000);
  });
});
