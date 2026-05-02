import { useEffect, useState } from "react";
import type { Report } from "../lib/types";
import { renderMarkdown } from "../lib/markdown";
import { HeaderBar } from "./report/HeaderBar";
import { RequestLine } from "./report/RequestLine";
import { MetricsGrid } from "./report/MetricsGrid";
import { TimingWaterfall } from "./report/TimingWaterfall";
import { FindingCard } from "./report/FindingCard";

// Re-run is universally available across surfaces (Run / HAR / Demos / extension).
// In Demos and HAR contexts it is conceptually a re-render of the same input,
// but exposing it everywhere keeps the action bar consistent and avoids per-tab
// component branching. The parent supplies onRerun; if absent the button is
// hidden (e.g., static rendering tests).
//
// Share is intentionally hidden in v1: it requires persistence (storing the
// Report so a share URL can retrieve it later), and persistence is explicitly
// cut from v1 per docs/architecture.md. Re-enable when persistence ships.
export interface ReportViewProps {
  report: Report;
  onRerun?: () => void | Promise<void>;
  rerunBusy?: boolean;
  rerunLabel?: string;
}

export function ReportView({
  report,
  onRerun,
  rerunBusy = false,
  rerunLabel = "Re-run",
}: ReportViewProps) {
  const findings = report.findings ?? [];
  const hasCritical = findings.some((f) => f.severity === "critical");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );

  useEffect(() => {
    if (copyState === "idle") return;
    const t = setTimeout(() => setCopyState("idle"), 2000);
    return () => clearTimeout(t);
  }, [copyState]);

  const onExport = async () => {
    const md = renderMarkdown(report);
    try {
      await navigator.clipboard.writeText(md);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

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

        <div className="flex flex-wrap items-center gap-2 mt-5 pt-4 border-t border-black/[0.12]">
          {onRerun ? (
            <ActionButton onClick={onRerun} disabled={rerunBusy}>
              {rerunBusy ? "Re-running…" : rerunLabel}
            </ActionButton>
          ) : null}
          <ActionButton onClick={onExport}>Export markdown</ActionButton>
          {copyState === "copied" ? (
            <span
              role="status"
              aria-live="polite"
              className="text-xs text-muted"
            >
              Copied to clipboard
            </span>
          ) : copyState === "failed" ? (
            <span
              role="status"
              aria-live="polite"
              className="text-xs text-red-700"
            >
              Copy failed
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

interface ActionButtonProps {
  children: React.ReactNode;
  onClick?: () => void | Promise<void>;
  disabled?: boolean;
}

function ActionButton({ children, onClick, disabled }: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="bg-transparent border border-black/25 text-ink text-[13px] px-3 py-1.5 rounded-lg hover:bg-black/[0.04] disabled:opacity-50"
    >
      {children}
    </button>
  );
}
