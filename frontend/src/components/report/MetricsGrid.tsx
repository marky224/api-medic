import type { Finding, ResponseSummary, Severity, TimingBreakdown } from "../../lib/types";

const SEVERITY_RANK: Record<Severity, number> = {
  critical: 3,
  warning: 2,
  info: 1,
};

function topSeverity(findings: Finding[]): Severity | null {
  let top: Severity | null = null;
  for (const f of findings) {
    if (!top || SEVERITY_RANK[f.severity] > SEVERITY_RANK[top]) top = f.severity;
  }
  return top;
}

// Only critical and warning get a colored tone — info findings are status,
// not issues, so they read as default ink alongside Latency/Body/Protocol.
const SEVERITY_TONE: Record<Severity, string> = {
  critical: "text-red-700",
  warning: "text-amber-700",
  info: "",
};

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatLatency(total: number | null | undefined): string {
  if (total == null) return "—";
  if (total < 1000) return `${Math.round(total)} ms`;
  return `${(total / 1000).toFixed(2)} s`;
}

interface MetricsGridProps {
  timing: TimingBreakdown;
  response: ResponseSummary | null | undefined;
  findings: Finding[];
}

export function MetricsGrid({ timing, response, findings }: MetricsGridProps) {
  const top = topSeverity(findings);
  const findingsCount = top
    ? findings.filter((f) => f.severity === top).length
    : findings.length;
  const findingsLabel = top
    ? `${findingsCount} ${top}`
    : findingsCount === 0
      ? "0"
      : `${findingsCount}`;
  const findingsTone = top ? SEVERITY_TONE[top] : "";

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-6">
      <Metric label="Latency" value={formatLatency(timing.total_ms)} />
      <Metric
        label="Body"
        value={response ? formatBytes(response.body_size_bytes) : "—"}
      />
      <Metric label="Protocol" value={response ? response.protocol : "—"} />
      <Metric label="Findings" value={findingsLabel} valueClassName={findingsTone} />
    </div>
  );
}

function Metric({
  label,
  value,
  valueClassName = "",
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div className="bg-sunken rounded-lg px-3 py-2.5">
      <div className="text-[11px] text-muted mb-1">{label}</div>
      <div className={`text-lg font-medium ${valueClassName}`}>{value}</div>
    </div>
  );
}
