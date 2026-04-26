interface HeaderBarProps {
  timestamp: string;
  hasCritical: boolean;
}

function formatTimestamp(iso: string): string {
  // 2026-04-25T14:23:00Z -> 2026-04-25 14:23 UTC
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mi = String(d.getUTCMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi} UTC`;
}

export function HeaderBar({ timestamp, hasCritical }: HeaderBarProps) {
  return (
    <div className="flex items-center justify-between mb-4">
      <div className="flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full ${
            hasCritical ? "bg-red-700" : "bg-emerald-600"
          }`}
          aria-hidden
        />
        <span className="text-sm font-medium">api-medic — diagnostic report</span>
      </div>
      <span className="text-xs text-muted">{formatTimestamp(timestamp)}</span>
    </div>
  );
}
