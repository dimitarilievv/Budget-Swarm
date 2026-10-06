# frontend/

React + TypeScript + Vite + TailwindCSS + Leaflet + Axios.
Scaffolded in BS-016. Pages: Input, Results (with map), Comparison, Auth (login/register).

## Authentication

- `/login` and `/register` render `src/ui/pages/AuthPage.tsx` (one screen, tabbed). A tiny
  client router (`src/router.ts`) keeps the tab switch in the URL without a reload; every
  other path is the existing single-view shell.
- `src/contexts/AuthContext.tsx` owns the session: `login`/`register`/`logout`, persists the
  token pair (`src/api/auth.ts`), attaches `Authorization: Bearer` to every request, and
  refreshes once on a `401` (rotating), clearing the session if that fails. So once signed
  in, `generate-trip` is sent authenticated (saved + booking links) automatically.
- "Keep me signed in" → tokens in `localStorage`; otherwise `sessionStorage`.
- After sign-in the user is redirected to `?next=` (same-site only) or `/`.
- Backend base URL is `VITE_API_BASE_URL` (defaults to `http://localhost:8000`).
- Not wired: "Forgot password?" (no backend reset flow yet) — the link is present but inert.
