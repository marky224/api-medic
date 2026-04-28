// Client-side HAR size reduction for the hosted demo.
//
// API Gateway's HTTP API has a 10 MB body cap. A normal browser-exported HAR
// from a content-heavy page easily blows past that. Rather than 413 the user,
// we shrink the payload here in three escalating tiers, then surface a banner
// describing what was dropped.
//
// The Lambda parser (api_medic.core.parser.parse_har) only ever reads
// log.entries[0], so the "first-entry only" tier is information-equivalent
// for v1 analysis — the report would be the same whether we sent 1 or 1000
// entries. Verify this invariant in core/parser.py before changing it.

export interface HarEntry {
  request?: {
    postData?: { text?: string; [k: string]: unknown } | null;
    [k: string]: unknown;
  };
  response?: {
    content?: { text?: string; [k: string]: unknown } | null;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export interface HarFile {
  log: {
    entries: HarEntry[];
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export type StripAction =
  | "passthrough"
  | "stripped-bodies"
  | "first-entry-only";

export interface StripSummary {
  action: StripAction;
  originalEntryCount: number;
  finalEntryCount: number;
  bodiesStripped: number;
  finalBytes: number;
  thresholdBytes: number;
}

// Conservative cap: API Gateway's hard limit is 10 MB. Leave headroom for the
// {kind:"har", har: ...} envelope plus any unicode-vs-bytes overhead.
export const DEFAULT_THRESHOLD_BYTES = 9_000_000;

export function encodedPayloadSize(har: HarFile): number {
  return new Blob([JSON.stringify({ kind: "har", har })]).size;
}

function clearBodyTexts(har: HarFile): number {
  let stripped = 0;
  for (const entry of har.log.entries) {
    const postData = entry.request?.postData;
    if (postData && typeof postData.text === "string" && postData.text !== "") {
      postData.text = "";
      stripped++;
    }
    const content = entry.response?.content;
    if (content && typeof content.text === "string" && content.text !== "") {
      content.text = "";
      stripped++;
    }
  }
  return stripped;
}

export class HarTooLargeError extends Error {
  constructor(public finalBytes: number, public thresholdBytes: number) {
    super(
      `HAR is still ${(finalBytes / 1_000_000).toFixed(1)} MB after stripping ` +
        `bodies and reducing to the first entry only — over the ${(
          thresholdBytes / 1_000_000
        ).toFixed(0)} MB upload limit. Try a smaller capture.`,
    );
    this.name = "HarTooLargeError";
  }
}

// Mutates `har` in place (cheaper than cloning a multi-MB object) and returns
// it alongside a summary for the UI banner. Callers should not reuse the
// pre-strip HAR after this returns.
export function prepareHarForUpload(
  har: HarFile,
  thresholdBytes: number = DEFAULT_THRESHOLD_BYTES,
): { har: HarFile; summary: StripSummary } {
  const originalEntryCount = har.log.entries.length;

  let size = encodedPayloadSize(har);
  if (size <= thresholdBytes) {
    return {
      har,
      summary: {
        action: "passthrough",
        originalEntryCount,
        finalEntryCount: originalEntryCount,
        bodiesStripped: 0,
        finalBytes: size,
        thresholdBytes,
      },
    };
  }

  const bodiesStripped = clearBodyTexts(har);
  size = encodedPayloadSize(har);
  if (size <= thresholdBytes) {
    return {
      har,
      summary: {
        action: "stripped-bodies",
        originalEntryCount,
        finalEntryCount: originalEntryCount,
        bodiesStripped,
        finalBytes: size,
        thresholdBytes,
      },
    };
  }

  if (har.log.entries.length > 1) {
    har.log.entries = har.log.entries.slice(0, 1);
  }
  size = encodedPayloadSize(har);
  if (size <= thresholdBytes) {
    return {
      har,
      summary: {
        action: "first-entry-only",
        originalEntryCount,
        finalEntryCount: har.log.entries.length,
        bodiesStripped,
        finalBytes: size,
        thresholdBytes,
      },
    };
  }

  throw new HarTooLargeError(size, thresholdBytes);
}
