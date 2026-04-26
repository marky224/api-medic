import type { Report } from "./types";

export interface FixtureMeta {
  id: string;
  filename: string;
}

const INDEX_URL = "/fixtures/index.json";
const reportUrl = (id: string) => `/fixtures/${id}.json`;

export async function listFixtures(): Promise<FixtureMeta[]> {
  const res = await fetch(INDEX_URL);
  if (!res.ok) {
    throw new Error(`Failed to list fixtures: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as FixtureMeta[];
}

export async function loadReport(id: string): Promise<Report> {
  const res = await fetch(reportUrl(id));
  if (!res.ok) {
    throw new Error(
      `Failed to load fixture '${id}': ${res.status} ${res.statusText}`,
    );
  }
  return (await res.json()) as Report;
}
