/**
 * NoTripFound — the friendly empty state shown when the engine returns NO_PLAN_AVAILABLE.
 *
 * Distinct from the generic {@link ErrorState}: a no-plan result isn't an error, it's an
 * outcome — no destination met every constraint within budget. So this reads as guidance, with
 * an on-brand illustration and two ways forward (both return to the form to adjust inputs, since
 * re-running the identical request would only reproduce the same result).
 */

function euro(value: number): string {
  return new Intl.NumberFormat("en-IE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
}

interface NoTripFoundProps {
  /** The budget the user asked for, echoed back so the message is concrete. */
  budget?: number | null;
  /** The server's own message, shown as supporting detail. */
  message?: string | null;
  /** Return to the form to change the constraints. */
  onAdjust?: () => void;
}

export default function NoTripFound({
  budget,
  message,
  onAdjust,
}: NoTripFoundProps) {
  return (
    <div className="animate-enter py-4 text-center" role="status">
      <SignpostArt className="mx-auto h-40 w-40" />

      <h2 className="mt-4 text-xl font-bold text-slate-800">
        No trip fits these constraints
      </h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-slate-600">
        We explored thousands of combinations, but couldn't find a trip that
        meets all of your requirements
        {budget != null && budget > 0 ? (
          <>
            {" "}
            and stays within your{" "}
            <span className="font-semibold text-slate-800">
              {euro(budget)}
            </span>{" "}
            budget
          </>
        ) : null}
        .
      </p>

      {message && (
        <p className="mx-auto mt-3 max-w-md rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">
          {message}
        </p>
      )}

      <div className="mt-6 flex flex-col items-center gap-3">
        {onAdjust && (
          <>
            <button
              type="button"
              onClick={onAdjust}
              className="rounded-xl bg-teal-600 px-6 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-teal-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/40"
            >
              Adjust your budget or preferences
            </button>
            <button
              type="button"
              onClick={onAdjust}
              className="text-sm font-semibold text-teal-700 underline-offset-4 transition hover:text-teal-800 hover:underline"
            >
              Try different preferences
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/** A small signpost-in-a-landscape illustration in the app's teal/amber palette. */
function SignpostArt({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 200 200"
      className={className}
      role="img"
      aria-label="Signpost with no matching route"
    >
      {/* soft backdrop */}
      <circle cx="100" cy="96" r="86" fill="#ecfdf8" />
      <circle cx="100" cy="96" r="86" fill="none" stroke="#ccfbf1" />

      {/* hills */}
      <path
        d="M22 138c22-30 40-30 58-8 14-18 30-26 50-14 18 11 30 8 48-4v42H22z"
        fill="#a7f3d0"
        opacity="0.6"
      />
      <path
        d="M22 150c26-22 44-16 62 2 16-14 34-16 52-4 12 8 24 8 42-2v18H22z"
        fill="#5eead4"
        opacity="0.55"
      />

      {/* signpost */}
      <rect x="96" y="60" width="8" height="86" rx="4" fill="#0f766e" />
      {/* teal arrow (left) */}
      <path
        d="M96 74H50l-12 11 12 11h46z"
        fill="#14b8a6"
        stroke="#0d9488"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      {/* amber arrow (right) */}
      <path
        d="M104 100h48l12 11-12 11h-48z"
        fill="#fbbf24"
        stroke="#f59e0b"
        strokeWidth="2"
        strokeLinejoin="round"
      />

      {/* magnifier */}
      <circle
        cx="132"
        cy="150"
        r="15"
        fill="#ffffff"
        stroke="#0d9488"
        strokeWidth="4"
      />
      <line
        x1="143"
        y1="161"
        x2="156"
        y2="174"
        stroke="#0d9488"
        strokeWidth="5"
        strokeLinecap="round"
      />
    </svg>
  );
}
