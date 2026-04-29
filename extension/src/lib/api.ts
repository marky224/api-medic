import type { Report } from "@frontend/lib/types";
import { buildAnalyzePayload, type HarEntryLike } from "./serialize";

const API_BASE = "https://api-medic.markandrewmarquez.com";

export async function analyzeHarEntry(entry: HarEntryLike): Promise<Report> {
  const payload = buildAnalyzePayload(entry);
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const j = (await res.json()) as { detail?: string };
      detail = j.detail;
    } catch {
      // body wasn't JSON
    }
    throw new Error(
      detail
        ? `Analyze failed: ${detail}`
        : `Analyze failed: ${res.status} ${res.statusText}`,
    );
  }
  return (await res.json()) as Report;
}
