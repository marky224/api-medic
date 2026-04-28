import { useState } from "react";
import { analyzeHar } from "../lib/api";
import type { StripSummary } from "../lib/harStrip";
import type { Report } from "../lib/types";
import { ReportView } from "./ReportView";

interface HarFileSummary {
  name: string;
  entryCount: number;
  firstUrl: string | null;
}

function summarizeHar(name: string, raw: string): HarFileSummary {
  const parsed = JSON.parse(raw) as unknown;
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    !("log" in parsed) ||
    typeof (parsed as { log: unknown }).log !== "object"
  ) {
    throw new Error("File is not a HAR archive (missing 'log' property).");
  }
  const log = (parsed as { log: { entries?: unknown } }).log;
  if (!Array.isArray(log.entries)) {
    throw new Error("HAR is missing 'log.entries' array.");
  }
  const entries = log.entries as Array<{ request?: { url?: string } }>;
  const firstUrl = entries[0]?.request?.url ?? null;
  return { name, entryCount: entries.length, firstUrl };
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () =>
      reject(reader.error ?? new Error("Failed to read file"));
    reader.readAsText(file);
  });
}

export function HarUpload() {
  const [file, setFile] = useState<File | null>(null);
  const [summary, setSummary] = useState<HarFileSummary | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [strip, setStrip] = useState<StripSummary | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const onFile = async (f: File) => {
    setParseError(null);
    setReport(null);
    setStrip(null);
    setRunError(null);
    try {
      const text = await readAsText(f);
      setSummary(summarizeHar(f.name, text));
      setFile(f);
    } catch (err) {
      setSummary(null);
      setFile(null);
      setParseError(err instanceof Error ? err.message : String(err));
    }
  };

  const onAnalyze = async () => {
    if (!file) return;
    setRunning(true);
    setRunError(null);
    setStrip(null);
    try {
      const result = await analyzeHar(file);
      setReport(result.report);
      setStrip(result.strip);
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="bg-panel rounded-xl p-5">
        <div className="bg-white rounded-xl border border-black/[0.12] p-5 flex flex-col gap-4">
          <div>
            <label
              htmlFor="har-file"
              className="block text-[13px] font-medium mb-2"
            >
              HAR file
            </label>
            <label
              htmlFor="har-file"
              className="block border border-dashed border-black/20 rounded-lg px-4 py-6 text-center cursor-pointer hover:bg-sunken/40"
            >
              <input
                id="har-file"
                type="file"
                accept=".har,application/json"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void onFile(f);
                }}
                className="sr-only"
              />
              {summary ? (
                <div className="text-sm">
                  <div className="font-medium">{summary.name}</div>
                  <div className="text-muted text-xs mt-1">
                    {summary.entryCount}{" "}
                    {summary.entryCount === 1 ? "entry" : "entries"}
                    {summary.firstUrl ? ` · first: ${summary.firstUrl}` : ""}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-muted">
                  Click to choose a .har file
                </div>
              )}
            </label>
            {parseError ? (
              <p className="text-xs text-red-700 mt-2">{parseError}</p>
            ) : null}
          </div>

          <div className="flex justify-end pt-3 border-t border-black/[0.08]">
            <button
              type="button"
              onClick={onAnalyze}
              disabled={running || !summary}
              className="bg-ink text-paper text-sm font-medium px-4 py-1.5 rounded-lg hover:bg-black disabled:opacity-50"
            >
              {running ? "Analyzing…" : "Analyze"}
            </button>
          </div>
        </div>
      </div>

      {runError ? (
        <div className="bg-red-50 text-red-700 rounded-lg p-3 text-sm">
          {runError}
        </div>
      ) : null}
      {strip && strip.action !== "passthrough" ? (
        <StripBanner strip={strip} />
      ) : null}
      {report ? <ReportView report={report} /> : null}
    </div>
  );
}

function StripBanner({ strip }: { strip: StripSummary }) {
  const finalMb = (strip.finalBytes / 1_000_000).toFixed(1);
  if (strip.action === "stripped-bodies") {
    return (
      <div
        role="status"
        className="bg-amber-50 text-amber-900 border border-amber-200 rounded-lg p-3 text-sm"
      >
        <div className="font-medium">HAR was trimmed before upload</div>
        <div className="mt-1">
          {strip.bodiesStripped} request/response{" "}
          {strip.bodiesStripped === 1 ? "body" : "bodies"} were stripped
          across {strip.finalEntryCount}{" "}
          {strip.finalEntryCount === 1 ? "entry" : "entries"} so the upload
          stays under the 10 MB API Gateway limit. Final payload: {finalMb} MB.
          Headers, URLs, and timing are intact.
        </div>
      </div>
    );
  }
  return (
    <div
      role="status"
      className="bg-amber-50 text-amber-900 border border-amber-200 rounded-lg p-3 text-sm"
    >
      <div className="font-medium">Only the first HAR entry was uploaded</div>
      <div className="mt-1">
        Reduced from {strip.originalEntryCount} entries to 1 (final payload:{" "}
        {finalMb} MB). API Medic v1 only inspects the first entry, so the
        report below is the same as if the full HAR had fit.
      </div>
    </div>
  );
}
