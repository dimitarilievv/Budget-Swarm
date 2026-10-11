/**
 * ProfilePage — the signed-in user's account screen (`/profile`).
 *
 * Three sections: account info (email, name, member-since — read-only), a change-password
 * form (wired to `POST /api/auth/change-password`, which rotates this client's tokens and
 * logs every other session out), and a "danger zone" that permanently deletes the account
 * (`DELETE /api/auth/me`) behind a type-your-email confirmation. On delete the local
 * session ends and we return home.
 *
 * Self-contained like {@link MyTrips}: it renders its own header with a back-to-planner
 * link rather than the app shell's navbar.
 */
import { useState } from "react";

import { ApiError } from "@/api/errors";
import { useAuth } from "@/contexts/AuthContext";
import { navigate } from "@/router";
import { IconUser } from "@/ui/components/common/Icons";

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

export default function ProfilePage() {
  const { user, changePassword, deleteAccount } = useAuth();

  // --- change-password form state ---
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
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
      <header className="border-b border-teal-950/[0.07] bg-[#f5fbfa]/75 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between px-6">
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
          <button
            type="button"
            onClick={() => navigate("/")}
            className="text-sm font-medium text-slate-600 transition hover:text-slate-900"
          >
            ← Back to planner
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-10">
        <h1 className="flex items-center gap-2.5 text-2xl font-bold tracking-tight">
          <IconUser className="h-6 w-6 text-teal-600" />
          Your account
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Manage your sign-in details and account.
        </p>

        {/* Account info */}
        <section className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Account
          </h2>
          <dl className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <dt className="text-xs font-medium text-slate-500">Email</dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">
                {user.email}
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium text-slate-500">Name</dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">
                {user.name || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium text-slate-500">
                Member since
              </dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">
                {formatDate(user.created_at)}
              </dd>
            </div>
          </dl>
        </section>

        {/* Change password */}
        <section className="mt-6 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Change password
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            Changing your password signs out every other device.
          </p>
          <form onSubmit={onChangePassword} className="mt-4 space-y-4">
            <Field
              id="current-password"
              label="Current password"
              value={current}
              onChange={setCurrent}
              autoComplete="current-password"
            />
            <Field
              id="new-password"
              label="New password"
              value={next}
              onChange={setNext}
              autoComplete="new-password"
              hint={`At least ${MIN_PASSWORD} characters.`}
            />
            <Field
              id="confirm-password"
              label="Confirm new password"
              value={confirm}
              onChange={setConfirm}
              autoComplete="new-password"
            />

            {pwError && (
              <p role="alert" className="text-sm font-medium text-rose-600">
                {pwError}
              </p>
            )}
            {pwOk && (
              <p role="status" className="text-sm font-medium text-teal-700">
                Password updated.
              </p>
            )}

            <button
              type="submit"
              disabled={pwBusy || !current || !next || !confirm}
              className="rounded-full bg-teal-600 px-5 py-2 text-sm font-semibold text-white transition hover:bg-teal-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pwBusy ? "Updating…" : "Update password"}
            </button>
          </form>
        </section>

        {/* Danger zone */}
        <section className="mt-6 rounded-2xl border border-rose-200 bg-rose-50/50 p-6">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-rose-600">
            Delete account
          </h2>
          <p className="mt-1 text-sm text-slate-600">
            Permanently delete your account and all saved trips. This cannot be
            undone.
          </p>

          {!confirmingDelete ? (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className="mt-4 rounded-full border border-rose-300 bg-white px-5 py-2 text-sm font-semibold text-rose-600 transition hover:bg-rose-100"
            >
              Delete account
            </button>
          ) : (
            <div className="mt-4 space-y-3">
              <label
                htmlFor="delete-confirm"
                className="block text-sm font-medium text-slate-700"
              >
                Type your email{" "}
                <span className="font-semibold">{user.email}</span> to confirm
              </label>
              <input
                id="delete-confirm"
                type="email"
                value={deleteEmail}
                onChange={(e) => setDeleteEmail(e.target.value)}
                autoComplete="off"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-rose-400 focus:ring-2 focus:ring-rose-200"
              />
              {deleteError && (
                <p role="alert" className="text-sm font-medium text-rose-600">
                  {deleteError}
                </p>
              )}
              <div className="flex gap-3">
                <button
                  type="button"
                  disabled={!deleteArmed || deleteBusy}
                  onClick={onDelete}
                  className="rounded-full bg-rose-600 px-5 py-2 text-sm font-semibold text-white transition hover:bg-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
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
                  className="rounded-full border border-slate-200 bg-white px-5 py-2 text-sm font-medium text-slate-700 transition hover:text-slate-900"
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

/** A labelled password input used by the change-password form. */
function Field({
  id,
  label,
  value,
  onChange,
  autoComplete,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
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
        type="password"
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal-400 focus:ring-2 focus:ring-teal-200"
      />
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}
