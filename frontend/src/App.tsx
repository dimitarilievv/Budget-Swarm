/**
 * Landing shell (BS-016). The Results / Comparison pages arrive in later tickets; this
 * hosts the trip Input page (BS-084), which submits to /api/generate-trip.
 */
import TripInputForm from "./components/TripInputForm";

export default function App() {
  return (
    <main className="flex min-h-screen items-start justify-center bg-slate-50 p-6 text-slate-900">
      <div className="mt-8 w-full max-w-xl rounded-2xl bg-white p-8 shadow-lg ring-1 ring-slate-200">
        <h1 className="text-center text-3xl font-bold tracking-tight text-indigo-600">
          BudgetSwarm
        </h1>
        <p className="mt-2 text-center text-slate-600">
          Tell us your constraints and we'll plan a trip within budget.
        </p>
        <div className="mt-6">
          <TripInputForm />
        </div>
      </div>
    </main>
  );
}
