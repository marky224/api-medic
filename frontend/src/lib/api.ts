import { prepareHarForUpload, type HarFile, type StripSummary } from "./harStrip";
import type { Report } from "./types";

// Resolution order:
//   1. VITE_API_BASE if set — explicit override for any environment
//   2. VITE_DEMO_MODE=1 → same-origin ("") so /api/* hits CloudFront which
//      routes to API Gateway (the hosted demo's wiring)
//   3. otherwise → http://localhost:8765 (two-process dev: FastAPI launched
//      via `python -m api_medic.web.server`)
const API_BASE = ((): string => {
  const explicit = import.meta.env.VITE_API_BASE as string | undefined;
  if (explicit !== undefined) return explicit;
  const demoMode = (import.meta.env.VITE_DEMO_MODE as string | undefined) === "1";
  return demoMode ? "" : "http://localhost:8765";
})();

export interface RunSpec {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: string | null;
}

export interface AnalyzeHarResult {
  report: Report;
  strip: StripSummary;
}

export async function runRequest(spec: RunSpec): Promise<Report> {
  const res = await fetch(`${API_BASE}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Run"));
  }
  return (await res.json()) as Report;
}

export async function analyzeHar(file: File): Promise<AnalyzeHarResult> {
  const text = await readAsText(file);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("File is not valid JSON.");
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    !("log" in parsed) ||
    typeof (parsed as { log: unknown }).log !== "object" ||
    (parsed as { log: unknown }).log === null ||
    !Array.isArray((parsed as { log: { entries?: unknown } }).log.entries)
  ) {
    throw new Error("File is not a HAR archive (missing 'log.entries').");
  }
  const { har: prepared, summary } = prepareHarForUpload(parsed as HarFile);
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "har", har: prepared }),
  });
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Analyze"));
  }
  return { report: (await res.json()) as Report, strip: summary };
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

async function readErrorMessage(res: Response, action: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string };
    if (body.detail) return `${action} failed: ${body.detail}`;
  } catch {
    // body wasn't JSON; fall through to status-based message
  }
  return `${action} failed: ${res.status} ${res.statusText}`;
}
