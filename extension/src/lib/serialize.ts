// Wraps a single chrome.devtools.network.Request (HAR-entry-shaped per the
// spec) into a minimal HAR payload acceptable to /api/analyze. The analyzer
// only inspects entries[0] (see src/api_medic/core/parser.py), so a single-
// entry HAR is information-equivalent. The entry is a masked copy: see
// redact.ts for what is masked before it leaves the browser.

import { redactEntry } from "./redact";

// A HAR-1.2 `log.entries[i]`-shaped object. The exact shape is not
// constrained at this boundary because both `chrome.devtools.network.Request`
// (a closed interface with no index signature) and ad-hoc test fixtures are
// valid; the analyzer parses them downstream.
export type HarEntryLike = unknown;

export interface AnalyzeHarPayload {
  kind: "har";
  har: {
    log: {
      version: string;
      creator: { name: string; version: string };
      entries: HarEntryLike[];
    };
  };
}

export const ANALYZER_CREATOR = {
  name: "api-medic-extension",
  version: "0.2.1",
};

export function buildAnalyzePayload(entry: HarEntryLike): AnalyzeHarPayload {
  return {
    kind: "har",
    har: {
      log: {
        version: "1.2",
        creator: ANALYZER_CREATOR,
        entries: [redactEntry(entry)],
      },
    },
  };
}
