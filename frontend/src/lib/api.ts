import type { Report } from "./types";

// Two-process dev: this hits the FastAPI server (default port 8765) launched
// by `python -m api_medic.web.server`. Override via VITE_API_BASE.
const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8765";

export interface RunSpec {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: string | null;
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

export async function analyzeHar(file: File): Promise<Report> {
  const text = await readAsText(file);
  let har: unknown;
  try {
    har = JSON.parse(text);
  } catch {
    throw new Error("File is not valid JSON.");
  }
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "har", har }),
  });
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Analyze"));
  }
  return (await res.json()) as Report;
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
