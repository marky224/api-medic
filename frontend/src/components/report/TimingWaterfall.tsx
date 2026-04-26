import type { TimingBreakdown } from "../../lib/types";

const PHASES: Array<{ key: keyof TimingBreakdown; label: string }> = [
  { key: "dns_ms", label: "DNS" },
  { key: "connect_ms", label: "Connect" },
  { key: "tls_ms", label: "TLS" },
  { key: "ttfb_ms", label: "TTFB" },
  { key: "download_ms", label: "Download" },
];

export function TimingWaterfall({ timing }: { timing: TimingBreakdown }) {
  const rows = PHASES.map(({ key, label }) => ({
    label,
    ms: timing[key],
  })).filter((r): r is { label: string; ms: number } => typeof r.ms === "number");

  if (rows.length === 0) {
    return (
      <p className="text-xs text-muted">
        Per-phase timing not available for this report.
      </p>
    );
  }

  // Width is per-phase / total, matching the design reference. Falls back to
  // the sum of recorded phases when total_ms is null (captured-mode HAR).
  const denom =
    timing.total_ms ?? rows.reduce((acc, r) => acc + r.ms, 0) ?? 0;

  return (
    <div className="flex flex-col gap-2">
      {rows.map((r) => {
        const pct = denom > 0 ? Math.max(2, Math.round((r.ms / denom) * 100)) : 0;
        return (
          <div
            key={r.label}
            className="grid grid-cols-[80px_1fr_60px] gap-3 items-center text-xs"
          >
            <span className="text-muted">{r.label}</span>
            <div className="h-1.5 bg-sunken rounded-[3px] overflow-hidden">
              <div
                className="h-full bg-blue-700"
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="text-right text-muted tabular-nums">
              {Math.round(r.ms)} ms
            </span>
          </div>
        );
      })}
    </div>
  );
}
