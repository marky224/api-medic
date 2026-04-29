import { useEffect, useState } from "react";
import { ReportView } from "@frontend/components/ReportView";
import type { Report } from "@frontend/lib/types";
import { analyzeHarEntry } from "./lib/api";
import type { CapturedRequest } from "./types";

const MAX_CAPTURED = 100;

export function App() {
  const [captured, setCaptured] = useState<CapturedRequest[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onRequest = (entry: chrome.devtools.network.Request) => {
      const next: CapturedRequest = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        entry,
      };
      setCaptured((prev) => {
        const updated = [...prev, next];
        return updated.length > MAX_CAPTURED
          ? updated.slice(updated.length - MAX_CAPTURED)
          : updated;
      });
    };
    chrome.devtools.network.onRequestFinished.addListener(onRequest);
    return () => {
      chrome.devtools.network.onRequestFinished.removeListener(onRequest);
    };
  }, []);

  const selected = captured.find((c) => c.id === selectedId) ?? null;

  const onAnalyze = async () => {
    if (!selected) return;
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const r = await analyzeHarEntry(selected.entry);
      setReport(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const onClear = () => {
    setCaptured([]);
    setSelectedId(null);
    setReport(null);
    setError(null);
  };

  return (
    <div className="min-h-screen p-4 flex flex-col gap-4">
      <header className="flex items-center justify-between">
        <h1 className="text-base font-semibold">api-medic</h1>
        <button
          type="button"
          onClick={onClear}
          className="text-xs text-muted hover:text-ink"
        >
          Clear
        </button>
      </header>

      {captured.length === 0 ? (
        <p className="text-sm text-muted">
          Make a request in this tab to capture it here.
        </p>
      ) : (
        <ul className="flex flex-col gap-1.5 bg-panel rounded-xl p-3 max-h-64 overflow-auto">
          {captured.map((c) => {
            const active = c.id === selectedId;
            return (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(c.id)}
                  className={
                    "w-full text-left flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs " +
                    (active
                      ? "bg-white border border-black/20"
                      : "hover:bg-white/60")
                  }
                >
                  <span className="font-mono font-medium w-12 shrink-0">
                    {c.entry.request.method}
                  </span>
                  <span className="font-mono w-12 shrink-0">
                    {c.entry.response?.status ?? "—"}
                  </span>
                  <span className="font-mono truncate text-muted">
                    {c.entry.request.url}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {selected && (
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-muted truncate">
            Selected: {selected.entry.request.method}{" "}
            {selected.entry.request.url}
          </span>
          <button
            type="button"
            onClick={onAnalyze}
            disabled={loading}
            className="text-xs bg-ink text-paper rounded-lg px-3 py-1.5 hover:opacity-90 disabled:opacity-50 shrink-0"
          >
            {loading ? "Analyzing…" : "Analyze with api-medic"}
          </button>
        </div>
      )}

      {error && (
        <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg p-2.5">
          {error}
        </div>
      )}

      {report && <ReportView report={report} />}
    </div>
  );
}
