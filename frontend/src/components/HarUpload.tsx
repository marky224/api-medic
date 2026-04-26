import { useState } from "react";
import { loadReport, type FixtureMeta } from "../lib/fixtures";
import type { Report } from "../lib/types";
import { ReportView } from "./ReportView";
import { FixturePicker } from "./FixturePicker";

const DEFAULT_FIXTURE = "04-cors-misconfigured";

interface HarFileSummary {
  name: string;
  entryCount: number;
  firstUrl: string | null;
}

interface HarUploadProps {
  fixtures: FixtureMeta[];
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

export function HarUpload({ fixtures }: HarUploadProps) {
  const [summary, setSummary] = useState<HarFileSummary | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const initialFixture = fixtures.some((f) => f.id === DEFAULT_FIXTURE)
    ? DEFAULT_FIXTURE
    : (fixtures[0]?.id ?? DEFAULT_FIXTURE);
  const [responseFixture, setResponseFixture] = useState(initialFixture);
  const [report, setReport] = useState<Report | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const onFile = async (file: File) => {
    setParseError(null);
    setReport(null);
    try {
      const text = await readAsText(file);
      setSummary(summarizeHar(file.name, text));
    } catch (err) {
      setSummary(null);
      setParseError(err instanceof Error ? err.message : String(err));
    }
  };

  const onAnalyze = async () => {
    setRunning(true);
    setRunError(null);
    try {
      const r = await loadReport(responseFixture);
      setReport(r);
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

          <div className="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-black/[0.08]">
            <FixturePicker
              id="har-fixture"
              label="Phase 2 — return fixture:"
              fixtures={fixtures}
              value={responseFixture}
              onChange={setResponseFixture}
            />
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
      {report ? <ReportView report={report} /> : null}
    </div>
  );
}
