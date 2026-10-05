/**
 * SearchingCard — the searching state for the Plan trip card (design 6a).
 *
 * Replaces the form card while a plan is generating (see {@link TripInputForm}). It brings
 * its own card chrome (white, radius 22, 4px teal top border, max-width 520), a header with
 * an "Edit" affordance, five connected agent steps, a live finding strip, a progress bar and
 * rotating tips.
 *
 * Props:
 *   progress  0–1 from the backend/polling (omit to use the built-in simulated timer).
 *   stage     0–4 current agent (omit to derive from progress).
 *   findings  array of 5 strings, one live finding per agent (optional).
 *   summary   e.g. "From Skopje · €1,000 · 5 days · 2 travelers".
 *   onEdit    called when "Edit" is clicked (cancels the request, restoring the form).
 *
 * Announced via role="status" aria-live="polite"; honours prefers-reduced-motion by turning
 * off the pulse and the transitions.
 */
import { createElement, useEffect, useState } from "react";

const TEAL = "#0f9488";

const STEPS = [
  { key: "scout", label: "Scouting destinations" },
  { key: "transport", label: "Comparing transport" },
  { key: "stays", label: "Finding places to stay" },
  { key: "weather", label: "Checking the weather" },
  { key: "opt", label: "Optimising your trip" },
];

const DEFAULT_FINDINGS = [
  "Looking for cities within reach…",
  "Checking flights, buses and trains…",
  "Scanning stays in matching cities…",
  "Pulling 5-day forecasts…",
  "Ranking matches and building your days…",
];

const TIPS = [
  "Flexible travelers get the best results: the more we can search, the better the match.",
  "Tick more interests for a wider choice of cities.",
  "Prices for stays and flights are live where available. The rest are labelled estimates.",
];

type Shape = [tag: "circle" | "path", attrs: React.SVGProps<SVGSVGElement>];

const PATHS: Record<string, Shape[]> = {
  scout: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["path", { d: "M15.5 8.5L13 13l-4.5 2.5L11 11z" }],
  ],
  transport: [
    ["circle", { cx: 6, cy: 18, r: 2 }],
    ["circle", { cx: 18, cy: 6, r: 2 }],
    ["path", { d: "M8 18h7a3.5 3.5 0 0 0 0-7H9a3.5 3.5 0 0 1 0-7h7" }],
  ],
  stays: [
    ["path", { d: "M3 19V6" }],
    ["path", { d: "M3 15h18v4" }],
    ["path", { d: "M21 15v-3a3 3 0 0 0-3-3h-7v6" }],
    ["circle", { cx: 7, cy: 11.5, r: 1.6 }],
  ],
  weather: [
    ["circle", { cx: 8, cy: 8, r: 3 }],
    ["path", { d: "M8 2v1.5M2 8h1.5M3.8 3.8l1 1M12.2 3.8l-1 1" }],
    [
      "path",
      { d: "M17.5 20H9a4 4 0 1 1 .9-7.9A5 5 0 0 1 19 14a3 3 0 0 1-1.5 6z" },
    ],
  ],
  opt: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["circle", { cx: 12, cy: 12, r: 5 }],
    ["circle", { cx: 12, cy: 12, r: 1.2, fill: "currentColor" }],
  ],
};

function Icon({
  name,
  size = 24,
  color,
}: {
  name: string;
  size?: number;
  color: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: "block", color }}
    >
      {PATHS[name].map(([tag, attrs], i) =>
        createElement(tag, { key: i, ...attrs }),
      )}
    </svg>
  );
}

/** True when the viewer asked for reduced motion; updates if the preference changes. */
function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" && !!window.matchMedia?.(query).matches,
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

// Simulated progress for when the backend doesn't report progress.
function useSimulatedProgress(enabled: boolean, duration = 18000): number {
  const [p, setP] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const start = Date.now();
    const id = setInterval(
      () => setP(Math.min(1, (Date.now() - start) / duration)),
      100,
    );
    return () => clearInterval(id);
  }, [enabled, duration]);
  return p;
}

// A 0.45–1 breathing value driving the active step's ring and the finding dot. Disabled (held
// steady) under reduced motion.
function usePulse(enabled: boolean): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => setT((x) => x + 0.1), 100);
    return () => clearInterval(id);
  }, [enabled]);
  return enabled ? 0.45 + 0.55 * Math.abs(Math.sin(t * 3.3)) : 0.6;
}

interface SearchingCardProps {
  progress?: number;
  stage?: number;
  findings?: string[];
  summary?: string;
  onEdit?: () => void;
}

export default function SearchingCard({
  progress,
  stage: stageProp,
  findings = DEFAULT_FINDINGS,
  summary = "From Skopje · €1,000 · 5 days · 2 travelers",
  onEdit,
}: SearchingCardProps) {
  const reduced = useReducedMotion();
  const sim = useSimulatedProgress(progress == null);
  const p = Math.max(0, Math.min(1, progress ?? sim));
  const done = p >= 1;
  const stage = done ? 5 : (stageProp ?? Math.min(4, Math.floor(p * 5)));
  const local = done ? 1 : p * 5 - Math.min(4, Math.floor(p * 5));
  const pulse = usePulse(!reduced);
  const pct = Math.round(p * 100);
  const motion = (value: string) => (reduced ? "none" : value);

  const [tip, setTip] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTip((i) => (i + 1) % TIPS.length), 6000);
    return () => clearInterval(id);
  }, []);

  const lineWidth = Math.min(1, (stage + (done ? 0 : local)) / 4) * 80;

  return (
    <div
      style={{
        width: "100%",
        maxWidth: 520,
        background: "#fff",
        borderRadius: 22,
        boxShadow: "0 24px 48px -24px rgba(15,80,72,.25)",
        padding: "26px 26px 22px",
        display: "flex",
        flexDirection: "column",
        gap: 22,
        borderTop: `4px solid ${TEAL}`,
        boxSizing: "border-box",
        fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif",
        color: "#12201e",
      }}
      role="status"
      aria-live="polite"
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 12,
        }}
      >
        <div>
          <div style={{ fontSize: 21, fontWeight: 800 }}>Plan your trip</div>
          <div style={{ fontSize: 13, color: "#5d6b69" }}>{summary}</div>
        </div>
        <button
          type="button"
          onClick={onEdit}
          style={{
            all: "unset",
            fontSize: 12,
            fontWeight: 600,
            color: "#0b7a70",
            whiteSpace: "nowrap",
            paddingTop: 6,
            cursor: "pointer",
          }}
        >
          ✎ Edit
        </button>
      </div>

      {/* Title */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
          textAlign: "center",
          paddingTop: 4,
        }}
      >
        <span
          style={{
            padding: "4px 11px",
            borderRadius: 99,
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: ".08em",
            background: done ? "#e7f6ee" : "#e6f5f2",
            color: done ? "#1f8a55" : "#0b7a70",
          }}
        >
          {done ? "✓ ALL AGENTS DONE" : "● 5 AGENTS WORKING"}
        </span>
        <h3
          style={{
            margin: 0,
            fontSize: 26,
            fontWeight: 800,
            letterSpacing: "-.025em",
            lineHeight: 1.15,
          }}
        >
          {done ? "Your trip is ready!" : "Our AI travel agents are on it"}
        </h3>
        <p
          style={{
            margin: 0,
            fontSize: 15,
            color: "#5d6b69",
            lineHeight: 1.55,
            maxWidth: 380,
          }}
        >
          {done
            ? "Opening your plan…"
            : "Crunching data from thousands of sources to craft your perfect trip."}
        </p>
      </div>

      {/* Connected steps */}
      <div
        style={{
          position: "relative",
          display: "grid",
          gridTemplateColumns: "repeat(5,minmax(0,1fr))",
          gap: 4,
        }}
      >
        <div
          style={{
            position: "absolute",
            left: "10%",
            right: "10%",
            top: 27,
            height: 3,
            borderRadius: 2,
            background: "#e8eeec",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: "10%",
            top: 27,
            height: 3,
            borderRadius: 2,
            background: TEAL,
            width: `${lineWidth}%`,
            transition: motion("width .2s linear"),
          }}
        />
        {STEPS.map((s, i) => {
          const d = i < stage;
          const a = i === stage;
          return (
            <div
              key={s.key}
              style={{
                position: "relative",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 10,
              }}
            >
              <div
                style={{
                  position: "relative",
                  zIndex: 1,
                  width: 56,
                  height: 56,
                  borderRadius: 28,
                  display: "grid",
                  placeItems: "center",
                  background: a ? TEAL : d ? "#e6f5f2" : "#f4f6f5",
                  border: a
                    ? "none"
                    : d
                      ? "2px solid #9fd8d0"
                      : "2px solid #e4ebe9",
                  boxShadow: a
                    ? `0 0 0 ${5 + pulse * 5}px rgba(15,148,136,${0.12 + pulse * 0.1}),0 10px 20px -8px rgba(15,148,136,.5)`
                    : "none",
                  transform: a ? "scale(1.08)" : "none",
                  transition: motion("background .3s, transform .3s"),
                  boxSizing: "border-box",
                }}
              >
                <Icon name={s.key} color={a ? "#fff" : d ? TEAL : "#9aa8a5"} />
                {d && (
                  <span
                    style={{
                      position: "absolute",
                      right: -3,
                      bottom: -3,
                      width: 20,
                      height: 20,
                      borderRadius: 10,
                      background: TEAL,
                      color: "#fff",
                      border: "2px solid #fff",
                      display: "grid",
                      placeItems: "center",
                      fontSize: 10,
                      fontWeight: 800,
                    }}
                  >
                    ✓
                  </span>
                )}
              </div>
              <span
                style={{
                  fontSize: 12,
                  lineHeight: 1.3,
                  textAlign: "center",
                  fontWeight: a ? 800 : 600,
                  color: a ? "#0b7a70" : d ? "#12201e" : "#9aa8a5",
                }}
              >
                {s.label}
              </span>
            </div>
          );
        })}
      </div>

      {/* Live finding */}
      <div
        style={{
          display: "flex",
          gap: 10,
          alignItems: "center",
          padding: "11px 14px",
          borderRadius: 12,
          background: "#f0faf8",
          border: "1px solid #cfe9e4",
        }}
      >
        <span
          style={{
            flex: "none",
            width: 8,
            height: 8,
            borderRadius: 4,
            background: TEAL,
            opacity: done ? 1 : pulse,
          }}
        />
        <span
          style={{ flex: 1, fontSize: 13, color: "#0d4a44", lineHeight: 1.45 }}
        >
          {done
            ? "Done. Your best match is ready."
            : findings[Math.min(4, stage)]}
        </span>
      </div>

      {/* Progress */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div
          style={{
            height: 8,
            borderRadius: 4,
            background: "#edf1f0",
            overflow: "hidden",
          }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            style={{
              width: `${pct}%`,
              height: "100%",
              borderRadius: 4,
              background: TEAL,
              transition: motion("width .2s linear"),
            }}
          />
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 13,
            color: "#5d6b69",
          }}
        >
          <span>
            {done
              ? "All done"
              : p > 0.75
                ? "Almost there… a few seconds left"
                : "This usually takes 10–25 seconds"}
          </span>
          <b style={{ color: "#12201e" }}>{pct}%</b>
        </div>
      </div>

      {/* Rotating tip */}
      <div
        style={{
          padding: "14px 16px",
          borderRadius: 14,
          background: "#f4f8f7",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <span style={{ fontSize: 13, lineHeight: 1.55, color: "#4a5856" }}>
          <b style={{ color: "#12201e" }}>Tip:</b> {TIPS[tip]}
        </span>
        <div style={{ display: "flex", gap: 5 }}>
          {TIPS.map((_, i) => (
            <span
              key={i}
              style={{
                width: i === tip ? 18 : 6,
                height: 6,
                borderRadius: 3,
                background: i === tip ? TEAL : "#d6e2df",
                transition: motion("width .3s"),
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
