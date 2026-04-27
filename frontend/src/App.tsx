import { useEffect, useState } from "react";
import { listFixtures, type FixtureMeta } from "./lib/fixtures";
import { Tabs, type TabDef } from "./components/Tabs";
import { FixtureBrowser } from "./components/FixtureBrowser";
import { RequestComposer } from "./components/RequestComposer";
import { HarUpload } from "./components/HarUpload";

type TabId = "demos" | "run" | "har";

// Captured-mode detection. The hosted demo's Vite build sets
// VITE_DEMO_MODE=1, which hides the Run tab — there's no live runner on
// the Lambda surface, so live requests aren't possible there.
const ENV_DEMO_MODE =
  (import.meta.env.VITE_DEMO_MODE as string | undefined) === "1";

interface AppProps {
  /** Override env detection. Used by tests; production passes nothing. */
  demoMode?: boolean;
}

export function App({ demoMode }: AppProps = {}) {
  const isDemoMode = demoMode ?? ENV_DEMO_MODE;

  const tabs: TabDef<TabId>[] = isDemoMode
    ? [
        { id: "demos", label: "Demos" },
        { id: "har", label: "HAR" },
      ]
    : [
        { id: "demos", label: "Demos" },
        { id: "run", label: "Run" },
        { id: "har", label: "HAR" },
      ];

  const [fixtures, setFixtures] = useState<FixtureMeta[]>([]);
  const [fixturesError, setFixturesError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("demos");

  useEffect(() => {
    let cancelled = false;
    listFixtures()
      .then((list) => {
        if (cancelled) return;
        setFixtures(list);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setFixturesError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const tagline = isDemoMode
    ? "Upload a HAR or browse the demo scenarios."
    : "Run a request, upload a HAR, or browse the demo scenarios.";

  return (
    <main className="min-h-screen bg-paper text-ink px-4 py-8 sm:px-5 sm:py-10">
      <div className="mx-auto max-w-3xl">
        <header className="mb-6">
          <h1 className="text-xl font-medium tracking-tight">api-medic</h1>
          <p className="mt-1 text-sm text-muted">{tagline}</p>
        </header>

        <Tabs current={tab} tabs={tabs} onChange={setTab} />

        {tab === "demos" ? (
          <>
            {fixturesError ? (
              <div className="bg-red-50 text-red-700 rounded-lg p-3 text-sm">
                {fixturesError}
              </div>
            ) : null}
            <FixtureBrowser fixtures={fixtures} />
          </>
        ) : tab === "run" && !isDemoMode ? (
          <RequestComposer />
        ) : (
          <HarUpload />
        )}
      </div>
    </main>
  );
}
