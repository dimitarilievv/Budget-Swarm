/**
 * App — the application shell.
 *
 * Wraps everything in an {@link ErrorBoundary} + {@link TripProvider} and renders the chrome
 * (Navbar + Footer) around the active page: the Home planner, the Results page once a trip has
 * been generated, or the destination Comparison. The generated trip lives in TripContext, so the
 * nav wordmark and "Back to planning" both clear it through the same hook.
 *
 * On every page/view change the shell moves focus to the <main> region and scrolls to the top,
 * so keyboard and screen-reader users aren't stranded at the previous scroll position.
 */
import { useEffect, useRef, useState } from "react";

import { TripProvider } from "@/contexts/TripContext";
import { useTripResult } from "@/hooks/useTripResult";
import ErrorBoundary from "@/ui/components/common/ErrorBoundary";
import Footer from "@/ui/components/common/Footer";
import Navbar from "@/ui/components/layout/Navbar";
import ComparisonPage from "@/ui/pages/Comparison";
import Home from "@/ui/pages/Home";
import ResultsPage from "@/ui/pages/Results";

type ResultView = "results" | "compare";

/** Which distinct screen is showing — drives focus/scroll reset on change. */
function activeScreen(hasResult: boolean, view: ResultView): string {
  if (!hasResult) return "home";
  return view;
}

function Shell() {
  const { result, request, clear } = useTripResult();
  const [view, setView] = useState<ResultView>("results");
  const mainRef = useRef<HTMLElement>(null);

  // A freshly generated (or cleared) trip always lands on the results view.
  useEffect(() => setView("results"), [result]);

  const canCompare = (result?.ranked_candidates.length ?? 0) > 0;
  const screen = activeScreen(Boolean(result), view);

  // On each screen change, reset scroll and move focus into the main region so assistive tech
  // announces the new page instead of leaving the caret where it was.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
    mainRef.current?.focus();
  }, [screen]);

  return (
    <div className="flex min-h-screen flex-col text-slate-900">
      <a
        href="#main"
        className="sr-only rounded-lg bg-teal-600 px-4 py-2 font-semibold text-white focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50"
      >
        Skip to content
      </a>
      <Navbar
        onHome={clear}
        onCompare={canCompare ? () => setView("compare") : undefined}
        compareActive={view === "compare"}
      />
      <main
        id="main"
        ref={mainRef}
        tabIndex={-1}
        className="flex-1 outline-none"
      >
        {result ? (
          <div className="mx-auto max-w-6xl px-6 py-10">
            {view === "compare" ? (
              <ComparisonPage
                response={result}
                request={request}
                onBack={() => setView("results")}
                onAction={() => setView("results")}
              />
            ) : (
              <ResultsPage
                response={result}
                request={request}
                onBack={clear}
                onPlanAnother={clear}
                onCompare={canCompare ? () => setView("compare") : undefined}
              />
            )}
          </div>
        ) : (
          <Home />
        )}
      </main>
      <Footer />
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <TripProvider>
        <Shell />
      </TripProvider>
    </ErrorBoundary>
  );
}
