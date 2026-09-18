/**
 * LoadingState — the "AI agents are on it" progress screen shown while a trip is being
 * generated (BS-086). The backend call is a single synchronous request, so the five agent
 * stages are a friendly, timed animation (they advance on a timer) rather than live
 * progress — they mirror the real pipeline (Scout → Transport → Stay → Weather → Optimize)
 * so the wait feels purposeful. Announced via role="status" for assistive tech.
 */
import { useEffect, useState } from "react";

const STAGES = [
  { icon: "🔭", label: "Scouting destinations…" },
  { icon: "🚗", label: "Comparing transport…" },
  { icon: "🏨", label: "Finding places to stay…" },
  { icon: "🌦️", label: "Checking the weather…" },
  { icon: "🎯", label: "Optimizing your trip…" },
];

const STAGE_MS = 2500;

export default function LoadingState() {
  const [active, setActive] = useState(0);

  useEffect(() => {
    const id = setInterval(
      () => setActive((i) => Math.min(i + 1, STAGES.length - 1)),
      STAGE_MS,
    );
    return () => clearInterval(id);
  }, []);

  const progress = Math.round(((active + 1) / STAGES.length) * 100);

  return (
    <div className="py-4 text-center" role="status" aria-live="polite">
      <h2 className="text-2xl font-bold text-slate-800">
        Our AI travel agents are on it! 🐝
      </h2>
      <p className="mx-auto mt-2 max-w-md text-slate-600">
        Crunching data from thousands of sources to craft your perfect trip.
      </p>

      <ol className="mt-8 flex items-start justify-between gap-1">
        {STAGES.map((s, i) => {
          const done = i < active;
          const current = i === active;
          return (
            <li
              key={s.label}
              className="flex flex-1 flex-col items-center gap-2"
            >
              <span
                className={`flex h-12 w-12 items-center justify-center rounded-full text-xl transition ${
                  current
                    ? "bg-teal-600 text-white shadow-md ring-4 ring-teal-500/20"
                    : done
                      ? "bg-teal-50 text-teal-700"
                      : "bg-slate-100 text-slate-400"
                }`}
              >
                {s.icon}
              </span>
              <span
                className={`text-xs leading-tight ${
                  current ? "font-semibold text-teal-700" : "text-slate-500"
                }`}
              >
                {s.label}
              </span>
            </li>
          );
        })}
      </ol>

      <div
        className="mt-8 h-2 w-full overflow-hidden rounded-full bg-slate-100"
        role="progressbar"
        aria-valuenow={progress}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className="h-full rounded-full bg-teal-600 transition-all duration-700"
          style={{ width: `${progress}%` }}
        />
      </div>
      <p className="mt-2 text-sm text-slate-500">
        Almost there… this usually takes 10–25 seconds.
      </p>

      <div className="mt-6 rounded-xl bg-teal-50/70 px-4 py-3 text-sm text-slate-600">
        <span className="font-semibold text-slate-800">Tip:</span> Flexible
        travelers get the best results — the more we can search, the better the
        match.
      </div>
    </div>
  );
}
