/**
 * Placeholder landing page (BS-016).
 * Real pages (Input, Results with map, Comparison) arrive in BS-017 / BS-083.
 * Uses Tailwind utility classes to verify the styling pipeline works.
 */
export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 text-slate-900 flex items-center justify-center p-6">
      <div className="max-w-xl w-full rounded-2xl bg-white shadow-lg ring-1 ring-slate-200 p-8 text-center">
        <h1 className="text-3xl font-bold tracking-tight text-indigo-600">
          BudgetSwarm
        </h1>
        <p className="mt-3 text-slate-600">
          Budget-constrained trip planning. The UI shell is up — trip input,
          results map, and destination comparison land in upcoming tickets.
        </p>
        <span className="mt-6 inline-block rounded-full bg-indigo-50 px-4 py-1.5 text-sm font-medium text-indigo-700">
          Frontend skeleton ready
        </span>
      </div>
    </main>
  );
}
