import type { Report } from "../lib/types";
import { HeaderBar } from "./report/HeaderBar";
import { RequestLine } from "./report/RequestLine";
import { MetricsGrid } from "./report/MetricsGrid";
import { TimingWaterfall } from "./report/TimingWaterfall";
import { FindingCard } from "./report/FindingCard";

export function ReportView({ report }: { report: Report }) {
  const findings = report.findings ?? [];
  const hasCritical = findings.some((f) => f.severity === "critical");

  return (
    <div className="bg-panel rounded-xl p-5">
      <div className="bg-white rounded-xl border border-black/[0.12] p-5">
        <HeaderBar
          timestamp={report.timestamp ?? ""}
          hasCritical={hasCritical}
        />
        <RequestLine request={report.request} response={report.response} />
        <MetricsGrid
          timing={report.timing}
          response={report.response}
          findings={findings}
        />

        <section className="mb-6">
          <h2 className="text-[13px] font-medium mb-2.5">Timing breakdown</h2>
          <TimingWaterfall timing={report.timing} />
        </section>

        <section className="mb-6">
          <h2 className="text-[13px] font-medium mb-2.5">Findings</h2>
          {findings.length > 0 ? (
            <div className="flex flex-col gap-2.5">
              {findings.map((f, i) => (
                <FindingCard key={`${f.id}-${i}`} finding={f} />
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted">
              No findings — request looks healthy.
            </p>
          )}
        </section>

        {/* Phase 3 wires these to share-by-URL / engine re-run / markdown renderer. */}
        <div className="flex flex-wrap gap-2 mt-5 pt-4 border-t border-black/[0.12]">
          <ActionButton>Share report</ActionButton>
          <ActionButton>Re-run</ActionButton>
          <ActionButton>Export markdown</ActionButton>
        </div>
      </div>
    </div>
  );
}

function ActionButton({ children }: { children: React.ReactNode }) {
  return (
    <button
      type="button"
      className="bg-transparent border border-black/25 text-ink text-[13px] px-3 py-1.5 rounded-lg hover:bg-black/[0.04]"
    >
      {children}
    </button>
  );
}
