/**
 * Landing shell (BS-016). The full Input / Results / Comparison pages arrive in later
 * tickets; this shows the origin control (browser geolocation) working end to end.
 */
import { useState } from "react";

import OriginInput, { type OriginValue } from "./components/OriginInput";

export default function App() {
  const [origin, setOrigin] = useState<OriginValue>({
    originLabel: "",
    originLat: null,
    originLon: null,
  });

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-6 text-slate-900">
      <div className="w-full max-w-xl rounded-2xl bg-white p-8 text-center shadow-lg ring-1 ring-slate-200">
        <h1 className="text-3xl font-bold tracking-tight text-indigo-600">
          BudgetSwarm
        </h1>
        <p className="mt-3 text-slate-600">
          Budget-constrained trip planning. The full trip input, results map,
          and destination comparison land in upcoming tickets.
        </p>

        <div className="mt-6">
          <OriginInput value={origin} onChange={setOrigin} />
        </div>

        <span className="mt-6 inline-block rounded-full bg-indigo-50 px-4 py-1.5 text-sm font-medium text-indigo-700">
          Frontend skeleton ready
        </span>
      </div>
    </main>
  );
}
