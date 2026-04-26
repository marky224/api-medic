import type { Finding } from "../../lib/types";
import { SeverityPill } from "./SeverityPill";

function formatEvidenceValue(v: unknown): string {
  if (v === null) return "null";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

export function FindingCard({ finding }: { finding: Finding }) {
  const evidenceEntries = finding.evidence
    ? Object.entries(finding.evidence)
    : [];

  return (
    <div className="bg-sunken rounded-lg px-3.5 py-3">
      <div className="flex items-center justify-between mb-1.5 gap-3">
        <span className="font-medium text-sm">{finding.title}</span>
        <SeverityPill severity={finding.severity} />
      </div>
      <p className="text-[13px] text-muted mb-2 leading-snug">
        {finding.explanation}
      </p>
      {evidenceEntries.length > 0 ? (
        <dl className="font-mono text-[11px] bg-white px-2.5 py-1.5 rounded-lg text-muted mb-2 border border-black/[0.08]">
          {evidenceEntries.map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <dt className="shrink-0">{k}:</dt>
              <dd className="break-all">{formatEvidenceValue(v)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {finding.suggested_fix ? (
        <p className="text-xs">
          <span className="text-muted">Suggested fix:</span>{" "}
          {finding.suggested_fix}
        </p>
      ) : null}
    </div>
  );
}
