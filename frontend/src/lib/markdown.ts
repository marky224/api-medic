// Markdown renderer for Reports. Mirrors src/api_medic/core/render/markdown.py
// so Export-markdown produces equivalent output across CLI and web/extension
// surfaces. Severity is conveyed by [CRITICAL]/[WARNING]/[INFO] tags rather
// than emoji to render cleanly in GitHub issues, Slack, or email. Text from
// the Report is escaped so it reads as text: text, cell and code match
// markdown.py's _text, _cell and _code. The escaping relies on each title,
// explanation and suggested fix starting with the check's own words, since
// it doesn't guard the start of a line (`#`, `>`, `-`, a fence, an indent).

import type { Finding, Report, Severity, TimingBreakdown } from "./types";

const SEVERITY_TAG: Record<Severity, string> = {
  critical: "[CRITICAL]",
  warning: "[WARNING]",
  info: "[INFO]",
};

export function renderMarkdown(report: Report): string {
  const parts: string[] = [];
  parts.push("# api-medic — diagnostic report");
  parts.push("");
  parts.push(
    `${code(`${report.request.method} ${report.request.url}`)} → ${statusStr(report)}`,
  );
  parts.push("");
  parts.push(metricsTable(report));
  parts.push("");
  const timing = timingTable(report.timing);
  if (timing) {
    parts.push("## Timing");
    parts.push("");
    parts.push(timing);
    parts.push("");
  }
  parts.push("## Findings");
  parts.push("");
  const findings = report.findings ?? [];
  if (findings.length === 0) {
    parts.push("_No findings — the request looks healthy._");
  } else {
    for (const f of findings) {
      parts.push(findingBlock(f));
      parts.push("");
    }
  }
  return parts.join("\n").trimEnd() + "\n";
}

function statusStr(report: Report): string {
  if (!report.response) return "_no response_";
  return code(`${report.response.status_code} ${report.response.status_text}`);
}

function metricsTable(report: Report): string {
  const latency = fmtLatency(report.timing.total_ms ?? null);
  const body = report.response
    ? fmtBytes(report.response.body_size_bytes)
    : "—";
  const protocol = report.response ? cell(report.response.protocol) : "—";
  const findings = fmtFindingsCount(report.findings ?? []);
  return (
    "| Latency | Body | Protocol | Findings |\n" +
    "|---------|------|----------|----------|\n" +
    `| ${latency} | ${body} | ${protocol} | ${findings} |`
  );
}

function timingTable(t: TimingBreakdown): string {
  const rows: Array<[string, number]> = [];
  const phases: Array<[string, number | null | undefined]> = [
    ["DNS", t.dns_ms],
    ["Connect", t.connect_ms],
    ["TLS", t.tls_ms],
    ["TTFB", t.ttfb_ms],
    ["Download", t.download_ms],
  ];
  for (const [label, val] of phases) {
    if (val !== null && val !== undefined) rows.push([label, val]);
  }
  if (rows.length === 0 && (t.total_ms === null || t.total_ms === undefined)) {
    return "";
  }
  const out = ["| Phase | Duration |", "|-------|---------:|"];
  for (const [label, val] of rows) {
    out.push(`| ${label} | ${val.toFixed(0)} ms |`);
  }
  if (t.total_ms !== null && t.total_ms !== undefined) {
    out.push(`| **Total** | **${t.total_ms.toFixed(0)} ms** |`);
  }
  return out.join("\n");
}

function findingBlock(f: Finding): string {
  const lines: string[] = [
    `### ${SEVERITY_TAG[f.severity]} ${text(f.title)}`,
    `**${code(f.id)}**`,
    "",
    text(f.explanation),
  ];
  if (f.evidence && Object.keys(f.evidence).length > 0) {
    lines.push("");
    lines.push("**Evidence:**");
    for (const [k, v] of Object.entries(f.evidence)) {
      lines.push(`- ${code(k)}: ${code(evidenceValue(v))}`);
    }
  }
  if (f.suggested_fix) {
    lines.push("");
    lines.push(`**Suggested fix:** ${text(f.suggested_fix)}`);
  }
  return lines.join("\n");
}

function evidenceValue(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (v === null || v === undefined) return "null";
  return JSON.stringify(v);
}

function fmtLatency(total: number | null): string {
  if (total === null || total === undefined) return "—";
  if (total < 1000) return `${total.toFixed(0)} ms`;
  return `${(total / 1000).toFixed(2)} s`;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtFindingsCount(findings: Finding[]): string {
  if (findings.length === 0) return "0";
  const counts: Record<Severity, number> = {
    critical: 0,
    warning: 0,
    info: 0,
  };
  for (const f of findings) counts[f.severity] += 1;
  if (counts.critical) return `${counts.critical} critical`;
  if (counts.warning) return `${counts.warning} warning`;
  return `${counts.info} info`;
}

const MARKUP = /\\(?=[!-\/:-@\[-`{-~])|[\[\]<]|&(?=#?[0-9A-Za-z]+;)/g;

function lineBreaks(value: string): string {
  return value.replace(/\r/g, "\\r").replace(/\n/g, "\\n");
}

function text(value: string): string {
  return lineBreaks(value).replace(MARKUP, (c) =>
    c === "<" ? "&lt;" : c === "&" ? "&amp;" : "\\" + c,
  );
}

function cell(value: string): string {
  return text(value).replace(/\|/g, "\\|");
}

function code(value: string): string {
  const v = lineBreaks(value);
  let longest = 0;
  for (const run of v.match(/`+/g) ?? []) longest = Math.max(longest, run.length);
  const fence = "`".repeat(longest + 1);
  const ends = v.slice(0, 1) + v.slice(-1);
  const pad = /[^ ]/.test(v) && (ends.includes("`") || ends === "  ") ? " " : "";
  return `${fence}${pad}${v}${pad}${fence}`;
}
