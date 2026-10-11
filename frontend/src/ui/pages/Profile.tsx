/**
 * ProfilePage — the signed-in user's account screen (`/profile`).
 *
 * A teal hero with the user's avatar + account info, a change-password card (wired to
 * `POST /api/auth/change-password`, which rotates this client's tokens and logs every
 * other session out), and a "danger zone" that permanently deletes the account
 * (`DELETE /api/auth/me`) behind a type-your-email confirmation. On delete the local
 * session ends and we return home.
 *
 * Self-contained like {@link MyTrips}: it renders its own top bar with a back-to-planner
 * link rather than the app shell's navbar.
 */
import { useState } from "react";

import { ApiError } from "@/api/errors";
import { useAuth } from "@/contexts/AuthContext";
import { navigate } from "@/router";
import {
  IconCalendar,
  IconCheck,
  IconUser,
} from "@/ui/components/common/Icons";

const MIN_PASSWORD = 8;

/** "12 October 2026" from an ISO timestamp, or "—" when unknown. */
function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
}

/** Up to two initials from a name, else the email's first letter. */
function initialsFor(name: string | null, email: string): string {
  const source = (name ?? "").trim();
  if (source) {
    const parts = source.split(/\s+/).slice(0, 2);
    return parts.map((p) => p[0]!.toUpperCase()).join("");
  }
  return (email[0] ?? "?").toUpperCase();
}

export default function ProfilePage() {
  const { user, logout, changePassword, deleteAccount } = useAuth();

  // --- change-password form state ---
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwOk, setPwOk] = useState(false);
  const [pwBusy, setPwBusy] = useState(false);

  // --- delete-account state ---
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteEmail, setDeleteEmail] = useState("");
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  if (!user) return null; // route guard renders this only when authenticated

  async function onChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwOk(false);
    setPwError(null);
    if (next.length < MIN_PASSWORD) {
      setPwError(`New password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    if (next !== confirm) {
      setPwError("New password and confirmation do not match.");
      return;
    }
    if (next === current) {
      setPwError("New password must differ from the current one.");
      return;
    }
    setPwBusy(true);
    try {
      await changePassword(current, next);
      setPwOk(true);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setPwError(
        err instanceof ApiError
          ? err.message
          : "Something went wrong. Please try again.",
      );
    } finally {
      setPwBusy(false);
    }
  }

  async function onDelete() {
    setDeleteError(null);
    setDeleteBusy(true);
    try {
      await deleteAccount();
      navigate("/"); // session is cleared; return to the planner
    } catch (err) {
      setDeleteError(
        err instanceof ApiError
          ? err.message
          : "Something went wrong. Please try again.",
      );
      setDeleteBusy(false);
    }
  }

  const deleteArmed =
    deleteEmail.trim().toLowerCase() === user.email.toLowerCase();

  return (
    <div className="min-h-screen bg-[#f3f7f6] text-slate-900">
      {/* Top bar */}
      <header className="sticky top-0 z-20 border-b border-teal-950/[0.07] bg-[#f5fbfa]/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-2xl items-center justify-between px-6">
          <button
            type="button"
            onClick={() => navigate("/")}
            className="flex items-center gap-2.5 rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/40"
          >
            <img
              src="/logo.svg"
              alt=""
              aria-hidden="true"
              className="h-9 w-9 rounded-xl shadow-sm"
            />
            <span className="text-[17px] font-bold tracking-tight">
              Budget<span className="text-teal-600">Swarm</span>
            </span>
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => navigate("/")}
              className="rounded-full px-3 py-1.5 text-sm font-medium text-slate-600 transition hover:bg-white/70 hover:text-slate-900"
            >
              ← Planner
            </button>
            <button
              type="button"
              onClick={() => logout()}
              className="rounded-full border border-slate-200 bg-white px-4 py-1.5 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:text-slate-900"
            >
              Log out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-8 sm:py-10">
        {/* Hero / identity */}
        <section className="overflow-hidden rounded-3xl bg-gradient-to-br from-teal-600 to-teal-800 p-7 text-white shadow-lg shadow-teal-900/10">
          <div className="flex items-center gap-5">
            <div
              aria-hidden="true"
              className="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-white/15 text-2xl font-bold ring-1 ring-white/25"
            >
              {initialsFor(user.name, user.email)}
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-2xl font-bold tracking-tight">
                {user.name || "Your account"}
              </h1>
              <p className="truncate text-sm text-teal-50/90">{user.email}</p>
            </div>
          </div>
          <div className="mt-5 flex flex-wrap gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-white/15 px-3 py-1 text-xs font-medium ring-1 ring-white/20">
              <IconCalendar className="h-3.5 w-3.5" />
              Member since {formatDate(user.created_at)}
            </span>
          </div>
        </section>

        {/* Change password */}
        <section className="mt-6 rounded-3xl border border-slate-200/80 bg-white p-6 shadow-sm sm:p-7">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="flex items-center gap-2 text-base font-semibold text-slate-900">
                <IconUser className="h-4 w-4 text-teal-600" />
                Change password
              </h2>
              <p className="mt-0.5 text-sm text-slate-500">
                Updating signs out every other device.
              </p>
            </div>
          </div>

          <form onSubmit={onChangePassword} className="mt-5 space-y-4">
            <Field
              id="current-password"
              label="Current password"
              value={current}
              onChange={setCurrent}
              reveal={showPw}
              autoComplete="current-password"
            />
            <Field
              id="new-password"
              label="New password"
              value={next}
              onChange={setNext}
              reveal={showPw}
              autoComplete="new-password"
              hint={`At least ${MIN_PASSWORD} characters.`}
            />
            <Field
              id="confirm-password"
              label="Confirm new password"
              value={confirm}
              onChange={setConfirm}
              reveal={showPw}
              autoComplete="new-password"
            />

            <label className="flex w-fit cursor-pointer items-center gap-2 text-sm text-slate-600">
              <input
                type="checkbox"
                checked={showPw}
                onChange={(e) => setShowPw(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-teal-600 focus:ring-teal-500"
              />
              Show passwords
            </label>

            {pwError && (
              <p
                role="alert"
                className="rounded-lg bg-rose-50 px-3 py-2 text-sm font-medium text-rose-700"
              >
                {pwError}
              </p>
            )}
            {pwOk && (
              <p
                role="status"
                className="flex items-center gap-1.5 rounded-lg bg-teal-50 px-3 py-2 text-sm font-medium text-teal-700"
              >
                <IconCheck className="h-4 w-4" />
                Password updated.
              </p>
            )}

            <button
              type="submit"
              disabled={pwBusy || !current || !next || !confirm}
              className="rounded-full bg-teal-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-teal-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pwBusy ? "Updating…" : "Update password"}
            </button>
          </form>
        </section>

        {/* Danger zone */}
        <section className="mt-6 rounded-3xl border border-rose-200 bg-rose-50/40 p-6 sm:p-7">
          <h2 className="text-base font-semibold text-rose-700">
            Delete account
          </h2>
          <p className="mt-0.5 text-sm text-slate-600">
            Permanently delete your account and all saved trips. This cannot be
            undone.
          </p>

          {!confirmingDelete ? (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className="mt-4 rounded-full border border-rose-300 bg-white px-5 py-2.5 text-sm font-semibold text-rose-600 shadow-sm transition hover:bg-rose-600 hover:text-white"
            >
              Delete account
            </button>
          ) : (
            <div className="mt-4 space-y-3 rounded-2xl border border-rose-200 bg-white p-4">
              <label
                htmlFor="delete-confirm"
                className="block text-sm font-medium text-slate-700"
              >
                Type your email{" "}
                <span className="font-semibold text-slate-900">
                  {user.email}
                </span>{" "}
                to confirm
              </label>
              <input
                id="delete-confirm"
                type="email"
                value={deleteEmail}
                onChange={(e) => setDeleteEmail(e.target.value)}
                autoComplete="off"
                placeholder={user.email}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-rose-400 focus:ring-2 focus:ring-rose-200"
              />
              {deleteError && (
                <p role="alert" className="text-sm font-medium text-rose-600">
                  {deleteError}
                </p>
              )}
              <div className="flex flex-wrap gap-3 pt-1">
                <button
                  type="button"
                  disabled={!deleteArmed || deleteBusy}
                  onClick={onDelete}
                  className="rounded-full bg-rose-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {deleteBusy ? "Deleting…" : "Delete permanently"}
                </button>
                <button
                  type="button"
                  disabled={deleteBusy}
                  onClick={() => {
                    setConfirmingDelete(false);
                    setDeleteEmail("");
                    setDeleteError(null);
                  }}
                  className="rounded-full border border-slate-200 bg-white px-5 py-2.5 text-sm font-medium text-slate-700 transition hover:text-slate-900"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

/** A labelled password input (with an optional reveal) used by the change-password form. */
function Field({
  id,
  label,
  value,
  onChange,
  reveal,
  autoComplete,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  reveal: boolean;
  autoComplete: string;
  hint?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        type={reveal ? "text" : "password"}
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm outline-none transition focus:border-teal-400 focus:ring-2 focus:ring-teal-200"
      />
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}
