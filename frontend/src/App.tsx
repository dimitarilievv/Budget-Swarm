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

import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { TripProvider } from "@/contexts/TripContext";
import { useTripResult } from "@/hooks/useTripResult";
import { navigate, nextTarget, useRoutePath } from "@/router";
import ErrorBoundary from "@/ui/components/common/ErrorBoundary";
import Footer from "@/ui/components/common/Footer";
import Navbar from "@/ui/components/layout/Navbar";
import AuthPage, { type AuthMode } from "@/ui/pages/AuthPage";
import ComparisonPage from "@/ui/pages/Comparison";
import Home from "@/ui/pages/Home";
import MyTripsPage from "@/ui/pages/MyTrips";
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
      {/* The shared chrome wraps the Results/Compare views; the Home page brings its own
          sticky anchor header + teal footer, so it renders full-bleed without them. */}
      {result && (
        <Navbar
          onHome={clear}
          onCompare={canCompare ? () => setView("compare") : undefined}
          compareActive={view === "compare"}
        />
      )}
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
      {result && <Footer />}
    </div>
  );
}

/**
 * The `/login` and `/register` screen, wiring {@link AuthPage} to the auth context and the
 * router: tab switches update the URL, and a successful sign-in redirects to `?next=` (or
 * home). Already-signed-in users are bounced away from the auth screen.
 */
function AuthRoute({ mode }: { mode: AuthMode }) {
  const { login, register, status } = useAuth();

  useEffect(() => {
    if (status === "authenticated") navigate(nextTarget());
  }, [status]);

  const params = new URLSearchParams(window.location.search);
  const notice =
    mode === "login" && params.get("registered")
      ? "Account created — please log in."
      : undefined;

  return (
    <AuthPage
      initialMode={mode}
      notice={notice}
      onModeChange={(m) => navigate(`/${m}${window.location.search}`)}
      onLogin={async ({ email, password, remember }) => {
        await login(email, password, remember);
        navigate(nextTarget());
      }}
      onRegister={async ({ name, email, password }) => {
        // Registration does not sign the user in — send them to the login tab (keeping
        // the ?next= return target), with a success notice.
        await register(email, password, name);
        const sp = new URLSearchParams(window.location.search);
        sp.set("registered", "1");
        navigate(`/login?${sp.toString()}`);
      }}
    />
  );
}

/** The signed-in-only saved-trips page; signed-out visitors are sent to login. */
function MyTripsRoute() {
  const { status } = useAuth();

  useEffect(() => {
    if (status === "unauthenticated") navigate("/login?next=/my-trips");
  }, [status]);

  if (status !== "authenticated") return null; // brief: while loading or before redirect
  return <MyTripsPage />;
}

/** Route the real URLs to their screens; everything else is the single-view shell. */
function Router() {
  const path = useRoutePath();
  if (path === "/login" || path === "/register") {
    return <AuthRoute mode={path === "/register" ? "register" : "login"} />;
  }
  if (path === "/my-trips") {
    return <MyTripsRoute />;
  }
  return <Shell />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <TripProvider>
          <Router />
        </TripProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}
