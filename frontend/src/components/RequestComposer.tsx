import { useState } from "react";
import { runRequest } from "../lib/api";
import type { Report } from "../lib/types";
import { ReportView } from "./ReportView";

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"];

interface HeaderRow {
  key: string;
  value: string;
}

export function RequestComposer() {
  const [method, setMethod] = useState("POST");
  const [url, setUrl] = useState("https://api.example.com/v1/users");
  const [headers, setHeaders] = useState<HeaderRow[]>([
    { key: "Authorization", value: "Bearer ..." },
    { key: "Content-Type", value: "application/json" },
  ]);
  const [body, setBody] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateHeader = (i: number, patch: Partial<HeaderRow>) => {
    setHeaders((rows) =>
      rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
    );
  };
  const addHeader = () => setHeaders((rows) => [...rows, { key: "", value: "" }]);
  const removeHeader = (i: number) =>
    setHeaders((rows) => rows.filter((_, idx) => idx !== i));

  const onRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const headersObj: Record<string, string> = {};
      for (const h of headers) {
        const k = h.key.trim();
        if (k) headersObj[k] = h.value;
      }
      const r = await runRequest({
        method,
        url,
        headers: headersObj,
        body: body || null,
      });
      setReport(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="bg-panel rounded-xl p-5">
        <div className="bg-white rounded-xl border border-black/[0.12] p-5 flex flex-col gap-4">
          <div className="flex flex-wrap gap-2">
            <select
              aria-label="Method"
              value={method}
              onChange={(e) => setMethod(e.target.value)}
              className="bg-white border border-black/20 rounded-lg text-sm px-2.5 py-1.5 font-medium"
            >
              {METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <input
              aria-label="URL"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://api.example.com/v1/users"
              className="flex-1 min-w-[200px] bg-white border border-black/20 rounded-lg text-sm px-2.5 py-1.5 font-mono"
            />
          </div>

          <div>
            <div className="text-[13px] font-medium mb-2">Headers</div>
            <div className="flex flex-col gap-1.5">
              {headers.map((row, i) => (
                <div key={i} className="flex gap-2">
                  <input
                    aria-label={`Header ${i + 1} name`}
                    value={row.key}
                    onChange={(e) => updateHeader(i, { key: e.target.value })}
                    placeholder="Header name"
                    className="flex-1 bg-white border border-black/20 rounded-lg text-xs px-2 py-1.5 font-mono"
                  />
                  <input
                    aria-label={`Header ${i + 1} value`}
                    value={row.value}
                    onChange={(e) => updateHeader(i, { value: e.target.value })}
                    placeholder="value"
                    className="flex-[2] bg-white border border-black/20 rounded-lg text-xs px-2 py-1.5 font-mono"
                  />
                  <button
                    type="button"
                    onClick={() => removeHeader(i)}
                    aria-label={`Remove header ${i + 1}`}
                    className="text-muted hover:text-ink text-sm px-2"
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addHeader}
                className="self-start text-xs text-blue-700 hover:underline mt-1"
              >
                + Add header
              </button>
            </div>
          </div>

          <div>
            <label
              htmlFor="composer-body"
              className="text-[13px] font-medium mb-2 block"
            >
              Body
            </label>
            <textarea
              id="composer-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={4}
              placeholder='{"name": "Alex Doe"}'
              className="w-full bg-white border border-black/20 rounded-lg text-xs px-2.5 py-2 font-mono"
            />
          </div>

          <div className="flex justify-end pt-3 border-t border-black/[0.08]">
            <button
              type="button"
              onClick={onRun}
              disabled={running}
              className="bg-ink text-paper text-sm font-medium px-4 py-1.5 rounded-lg hover:bg-black disabled:opacity-50"
            >
              {running ? "Running…" : "Run"}
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <div className="bg-red-50 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      ) : null}
      {report ? <ReportView report={report} /> : null}
    </div>
  );
}
