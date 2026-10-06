/**
 * Minimal client-side routing.
 *
 * The app is otherwise a single-view SPA (state-driven, no router dependency). Auth adds a
 * couple of real URLs (`/login`, `/register`) that must be deep-linkable and keep the tab
 * switch in the address bar without a reload. This provides just that: a `navigate` that
 * pushes history and a hook that re-renders on path changes (both back/forward and
 * programmatic `navigate`).
 */
import { useEffect, useState } from "react";

const NAV_EVENT = "bs:navigate";

/** Push a new path and notify subscribers (no full-page reload). */
export function navigate(to: string): void {
  window.history.pushState({}, "", to);
  window.dispatchEvent(new Event(NAV_EVENT));
}

/** Current `location.pathname`, kept in sync with history + `navigate`. */
export function useRoutePath(): string {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onChange = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onChange);
    window.addEventListener(NAV_EVENT, onChange);
    return () => {
      window.removeEventListener("popstate", onChange);
      window.removeEventListener(NAV_EVENT, onChange);
    };
  }, []);
  return path;
}

/** The `?next=` redirect target for a post-auth redirect, defaulting to `/`. */
export function nextTarget(): string {
  const next = new URLSearchParams(window.location.search).get("next");
  // Only allow same-site relative paths, never an absolute/external URL (open-redirect guard).
  if (next && next.startsWith("/") && !next.startsWith("//")) return next;
  return "/";
}
