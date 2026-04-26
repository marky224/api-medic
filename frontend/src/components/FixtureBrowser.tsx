import { useEffect, useState } from "react";
import { loadReport, type FixtureMeta } from "../lib/fixtures";
import type { Report } from "../lib/types";
import { ReportView } from "./ReportView";

const DEFAULT_FIXTURE = "02-jwt-expired";

type LoadState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; report: Report }
  | { kind: "error"; message: string };

interface FixtureBrowserProps {
  fixtures: FixtureMeta[];
}

export function FixtureBrowser({ fixtures }: FixtureBrowserProps) {
  const initial = fixtures.some((f) => f.id === DEFAULT_FIXTURE)
    ? DEFAULT_FIXTURE
    : (fixtures[0]?.id ?? DEFAULT_FIXTURE);
  const [selected, setSelected] = useState<string>(initial);
  const [state, setState] = useState<LoadState>({ kind: "idle" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    loadReport(selected)
      .then((report) => {
        if (cancelled) return;
        setState({ kind: "ready", report });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <label htmlFor="fixture-select" className="text-sm text-muted">
          Fixture:
        </label>
        <select
          id="fixture-select"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="bg-white border border-black/20 rounded-lg text-sm px-2.5 py-1.5"
        >
          {fixtures.length === 0 ? (
            <option value={selected}>{selected}</option>
          ) : (
            fixtures.map((f) => (
              <option key={f.id} value={f.id}>
                {f.id}
              </option>
            ))
          )}
        </select>
      </div>

      {state.kind === "loading" || state.kind === "idle" ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : state.kind === "error" ? (
        <div className="bg-red-50 text-red-700 rounded-lg p-3 text-sm">
          {state.message}
        </div>
      ) : (
        <ReportView report={state.report} />
      )}
    </div>
  );
}
