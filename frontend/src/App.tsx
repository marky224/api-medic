import { useEffect, useState } from "react";
import { listFixtures, type FixtureMeta } from "./lib/fixtures";
import { Tabs, type TabDef } from "./components/Tabs";
import { FixtureBrowser } from "./components/FixtureBrowser";
import { RequestComposer } from "./components/RequestComposer";
import { HarUpload } from "./components/HarUpload";

type TabId = "demos" | "run" | "har";

const TABS: TabDef<TabId>[] = [
  { id: "demos", label: "Demos" },
  { id: "run", label: "Run" },
  { id: "har", label: "HAR" },
];

export function App() {
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

  return (
    <main className="min-h-screen bg-paper text-ink px-4 py-8 sm:px-5 sm:py-10">
      <div className="mx-auto max-w-3xl">
        <header className="mb-6">
          <h1 className="text-xl font-medium tracking-tight">api-medic</h1>
          <p className="mt-1 text-sm text-muted">
            Run a request, upload a HAR, or browse the demo scenarios.
          </p>
        </header>

        <Tabs current={tab} tabs={TABS} onChange={setTab} />

        {tab === "demos" ? (
          <>
            {fixturesError ? (
              <div className="bg-red-50 text-red-700 rounded-lg p-3 text-sm">
                {fixturesError}
              </div>
            ) : null}
            <FixtureBrowser fixtures={fixtures} />
          </>
        ) : tab === "run" ? (
          <RequestComposer />
        ) : (
          <HarUpload />
        )}
      </div>
    </main>
  );
}
