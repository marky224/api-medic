import type { Severity } from "../../lib/types";

const TONE: Record<Severity, string> = {
  critical: "bg-red-50 text-red-700",
  warning: "bg-amber-50 text-amber-700",
  info: "bg-blue-50 text-blue-700",
};

const LABEL: Record<Severity, string> = {
  critical: "Critical",
  warning: "Warning",
  info: "Info",
};

export function SeverityPill({ severity }: { severity: Severity }) {
  return (
    <span
      className={`text-[11px] px-2 py-[2px] rounded-lg font-medium whitespace-nowrap ${TONE[severity]}`}
    >
      {LABEL[severity]}
    </span>
  );
}
