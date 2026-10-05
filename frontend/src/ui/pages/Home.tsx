/**
 * Home — the landing / planner page, redesigned as a full scrolling page (hero + form,
 * How it works, Why us, FAQ, footer).
 *
 * Only the layout and styling are new: the planner is the existing {@link TripInputForm}
 * (all validation, geolocation, the API call, loading/error states), embedded in the hero
 * card. A successful submit stores the trip via {@link useTripResult} and the shell switches
 * to Results. The page brings its own sticky anchor header + teal footer (the shared Navbar/
 * Footer are shown on the Results/Compare views instead).
 */
import { useEffect, useRef, useState } from "react";

import TripInputForm from "@/ui/components/forms/TripInputForm";
import { useTripResult } from "@/hooks/useTripResult";

const NAV: [string, string][] = [
  ["plan", "Plan trip"],
  ["how", "How it works"],
  ["why", "Why us"],
  ["faq", "FAQ"],
];

/** Track which section is in view so the header nav can mark the current one. */
function useActiveSection(ids: string[]): string {
  const [active, setActive] = useState(ids[0]);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
        if (visible[0]) setActive(visible[0].target.id);
      },
      { rootMargin: "-45% 0px -50% 0px", threshold: [0, 0.2, 0.5, 1] },
    );
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.join(",")]);
  return active;
}

/** True when the viewer prefers reduced motion — we then skip the scroll animations. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Reveals an element the first time it scrolls into view. Returns false until then; returns
 * true immediately where IntersectionObserver is unavailable (SSR/tests) or motion is reduced,
 * so content is never left hidden.
 */
function useInView(ref: React.RefObject<Element | null>): boolean {
  const [inView, setInView] = useState(false);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined" || prefersReducedMotion()) {
      setInView(true);
      return;
    }
    const el = ref.current;
    if (!el) {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            setInView(true);
            io.disconnect();
            return;
          }
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return inView;
}

type RevealProps = React.HTMLAttributes<HTMLDivElement> & {
  /** Stagger, in ms, so items in a group cascade rather than pop at once. */
  delay?: number;
  /** How far (px) the element rises as it fades in. */
  y?: number;
};

/**
 * A plain <div> that fades and rises into place when scrolled into view. It merges the
 * reveal transition with any transition already on `style`, so existing hover/state
 * animations keep working, and honours prefers-reduced-motion (renders in place, no motion).
 */
function Reveal({ children, delay = 0, y = 26, style, ...rest }: RevealProps) {
  const ref = useRef<HTMLDivElement>(null);
  const shown = useInView(ref);
  const reveal: React.CSSProperties = {
    opacity: shown ? 1 : 0,
    transform: shown ? "none" : `translateY(${y}px)`,
    transition: [
      style?.transition,
      `opacity .7s ease ${delay}ms`,
      `transform .75s cubic-bezier(.22,.7,.2,1) ${delay}ms`,
    ]
      .filter(Boolean)
      .join(", "),
    willChange: "opacity, transform",
  };
  return (
    <div ref={ref} {...rest} style={{ ...style, ...reveal }}>
      {children}
    </div>
  );
}

/** True once the page has scrolled past `threshold` px — used to lift the sticky header. */
function useScrolled(threshold = 8): boolean {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > threshold);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [threshold]);
  return scrolled;
}

const C = {
  teal: "#0f9488",
  tealDark: "#0b7a70",
  tealTint: "#e6f5f2",
  ink: "#12201e",
  muted: "#5d6b69",
  grey: "#525252",
  line: "#e4ebe9",
  soft: "#f1f5f4",
};

const WRAP: React.CSSProperties = {
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 32px",
};
const EYEBROW: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 700,
  letterSpacing: ".14em",
  color: C.tealDark,
  textTransform: "uppercase",
};
const H2: React.CSSProperties = {
  margin: 0,
  fontSize: "clamp(32px,4.4vw,50px)",
  lineHeight: 1.05,
  letterSpacing: "-.035em",
  fontWeight: 800,
};

const STEPS = [
  {
    who: "You",
    time: "~30 sec",
    title: "Set your budget",
    text: "Tell us your total budget, how long you're going, how you'll travel and what you enjoy. No dates or destination needed.",
  },
  {
    who: "The swarm",
    time: "~20 sec",
    title: "Agents search in parallel",
    text: "Specialised AI agents check transport, stays, weather and city highlights for dozens of destinations at the same time.",
  },
  {
    who: "Your trip",
    time: "Instant",
    title: "Get one optimised trip",
    text: "One best destination, transport and a stay you can book, and a day-by-day plan that fits your wallet.",
  },
];

const COMPARE: [string, string, string][] = [
  ["Time to plan", "3–5 hours", "Under a minute"],
  ["Destinations compared", "2–3 you already know", "30 cities, ranked"],
  ["Budget check", "Spreadsheet guesswork", "Every euro tracked"],
  ["Day-by-day plan", "Build it yourself", "Ready, with a map"],
];

const TABS = [
  "Flight search",
  "Hotel deals",
  "Weather 10-day",
  "Bus timetable",
  "Maps",
  "Reviews",
  "Budget.xlsx",
  '"Top 10 things…"',
  "Currency",
  "Fuel calculator",
  "Hostel site",
  "Forum thread",
];
const TILT = [-3, 2, -1, 3, -2, 1, -3, 2, 0, -2, 3, -1];

const FAQS: [string, string, string][] = [
  [
    "Is BudgetSwarm free?",
    "Yes. Planning a trip is free. We link you to providers like Airbnb and airlines when you want to book.",
    "Pricing",
  ],
  [
    "How accurate are the prices?",
    "Flights and stays are live where available. Everything else is a careful estimate, and we label what's estimated.",
    "Planning",
  ],
  [
    "Do I need a destination in mind?",
    "No, that's the point. Give us a budget and what you enjoy, and we pick the destination for you.",
    "Planning",
  ],
  [
    "Can I travel by car or bus?",
    "Yes. Choose your transport mode, and for car trips we use your fuel consumption to calculate the cost.",
    "Travel",
  ],
  [
    "What does the swarm actually do?",
    "Several AI agents work at once — one checks transport, one checks stays, one checks weather and one checks highlights. A coordinator then scores every destination against your budget.",
    "Planning",
  ],
  [
    "Can I compare other destinations?",
    "Every plan includes a comparison with the next best cities, so you can switch if one suits you better.",
    "Pricing",
  ],
];

export default function Home() {
  const { setResult } = useTripResult();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        background: "#f6faf9",
        color: C.ink,
        fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif",
      }}
    >
      <Header />
      <Hero onResult={setResult} />
      <HowItWorks />
      <WhyUs />
      <Faq />
      <Footer />
    </div>
  );
}

/* ---------------- Header ---------------- */

function Header() {
  const active = useActiveSection(["plan", "how", "why", "faq"]);
  const scrolled = useScrolled(8);
  const link: React.CSSProperties = {
    padding: "7px 14px",
    color: C.muted,
    borderRadius: 9,
    textDecoration: "none",
    whiteSpace: "nowrap",
    fontSize: 13,
    fontWeight: 500,
  };
  const activeLink: React.CSSProperties = {
    ...link,
    background: "#fff",
    color: C.ink,
    fontWeight: 600,
    boxShadow: "0 1px 2px rgba(0,0,0,.06)",
  };
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 10,
        background: scrolled
          ? "rgba(246,250,249,.95)"
          : "rgba(246,250,249,.85)",
        backdropFilter: "blur(10px)",
        borderBottom: `1px solid rgba(15,148,136,${scrolled ? 0.18 : 0.1})`,
        boxShadow: scrolled ? "0 8px 24px -16px rgba(15,80,72,.45)" : "none",
        transition:
          "box-shadow .25s ease, background .25s ease, border-color .25s ease",
      }}
    >
      <div
        style={{
          ...WRAP,
          padding: "14px 32px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 20,
        }}
      >
        <button
          type="button"
          aria-label="BudgetSwarm home"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
          style={{
            all: "unset",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span
            style={{
              width: 32,
              height: 32,
              borderRadius: 9,
              background: C.teal,
              color: "#fff",
              display: "grid",
              placeItems: "center",
              fontWeight: 800,
            }}
          >
            B
          </span>
          <span style={{ fontWeight: 700, fontSize: 17 }}>
            Budget<span style={{ color: C.teal }}>Swarm</span>
          </span>
        </button>
        <nav style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {NAV.map(([id, label]) => {
            const on = active === id;
            return (
              <a
                key={id}
                href={`#${id}`}
                aria-current={on ? "page" : undefined}
                style={on ? activeLink : link}
              >
                {label}
              </a>
            );
          })}
        </nav>
        <button
          type="button"
          style={{
            all: "unset",
            cursor: "pointer",
            padding: "7px 14px",
            borderRadius: 99,
            border: "1px solid #d7e2df",
            fontSize: 13,
            background: "#fff",
            color: C.ink,
            whiteSpace: "nowrap",
          }}
        >
          Profile
        </button>
      </div>
    </header>
  );
}

/* ---------------- Hero + form ---------------- */

function Hero({
  onResult,
}: {
  onResult: Parameters<typeof TripInputForm>[0]["onResult"];
}) {
  return (
    <section
      id="plan"
      style={{
        background:
          "linear-gradient(160deg,#f3f6ea 0%,#eaf6f3 45%,#dff3f0 100%)",
        scrollMarginTop: 60,
      }}
    >
      <div
        style={{ maxWidth: 1200, margin: "0 auto", padding: "32px 32px 48px" }}
        className="grid items-center gap-8 lg:gap-14 lg:grid-cols-[1fr_minmax(0,440px)]"
      >
        <Reveal style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          <span
            style={{
              alignSelf: "start",
              padding: "5px 12px",
              borderRadius: 99,
              background: "#d9f3ee",
              color: C.tealDark,
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: ".12em",
              whiteSpace: "nowrap",
            }}
          >
            SMART ITINERARIES, BETTER BUDGETS
          </span>
          <h1
            style={{
              margin: 0,
              fontSize: "clamp(38px,5vw,56px)",
              lineHeight: 1.04,
              letterSpacing: "-.035em",
              fontWeight: 800,
            }}
          >
            Tell us your budget.
            <br />
            We'll find <span style={{ color: C.teal }}>where to go.</span>
          </h1>
          <p
            style={{
              margin: 0,
              fontSize: 17,
              lineHeight: 1.6,
              color: "#4a5856",
              maxWidth: 470,
            }}
          >
            BudgetSwarm uses AI to analyse travel costs, transport logistics and
            city highlights, then builds the perfect trip that respects your
            wallet.
          </p>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap" }}>
            {(
              [
                ["#0f9488", "Data-driven", "Real costs & routes"],
                ["#f59e0b", "Optimised", "One plan, within budget"],
              ] as [string, string, string][]
            ).map(([c, a, b]) => (
              <div key={a} style={{ display: "flex", gap: 10 }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 4,
                    background: c,
                    marginTop: 6,
                  }}
                />
                <div>
                  <div style={{ fontSize: 12, color: C.muted }}>{a}</div>
                  <div style={{ fontSize: 15, fontWeight: 600 }}>{b}</div>
                </div>
              </div>
            ))}
          </div>
          <a
            href="#how"
            style={{
              alignSelf: "start",
              fontSize: 14,
              fontWeight: 600,
              color: C.tealDark,
              textDecoration: "none",
              marginTop: 8,
            }}
          >
            See how it works ↓
          </a>
        </Reveal>

        {/* The real planner — all form logic, validation, geolocation, API call. It owns its
            own card chrome, and swaps to the searching card while a plan is generating. */}
        <Reveal delay={140}>
          <TripInputForm onResult={onResult} />
        </Reveal>
      </div>
    </section>
  );
}

/* ---------------- How it works ---------------- */

function HowItWorks() {
  const bg = "#cde8e1";
  return (
    <section id="how" style={{ background: bg, scrollMarginTop: 60 }}>
      <div
        style={{
          ...WRAP,
          padding: "112px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 64,
        }}
      >
        <Reveal
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,360px),1fr))",
            gap: "24px 56px",
            alignItems: "end",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <span style={{ ...EYEBROW, color: C.teal }}>How it works</span>
            <h2 style={H2}>
              Budget in.
              <br />
              Trip out.
            </h2>
          </div>
          <p
            style={{
              margin: 0,
              fontSize: 17,
              color: C.muted,
              lineHeight: 1.65,
              maxWidth: 440,
            }}
          >
            You tell us what you can spend. A swarm of AI agents does the hours
            of tab-hopping for you, and hands back one plan in under a minute.
          </p>
        </Reveal>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,260px),1fr))",
            gap: "40px 0",
          }}
        >
          {STEPS.map((s, i) => {
            const last = i === STEPS.length - 1;
            return (
              <Reveal
                key={s.title}
                delay={i * 110}
                style={{ display: "flex", flexDirection: "column", gap: 22 }}
              >
                <div style={{ display: "flex", alignItems: "center" }}>
                  <span
                    style={{
                      flex: "none",
                      width: 56,
                      height: 56,
                      borderRadius: 28,
                      display: "grid",
                      placeItems: "center",
                      fontSize: 20,
                      fontWeight: 800,
                      background: last ? C.teal : "#fff",
                      color: last ? "#fff" : C.teal,
                      border: last ? "none" : `2px solid ${C.teal}`,
                      boxShadow: `0 0 0 8px ${bg}`,
                    }}
                  >
                    {i + 1}
                  </span>
                  {!last && (
                    <span
                      style={{
                        flex: 1,
                        height: 2,
                        marginLeft: 8,
                        background: `repeating-linear-gradient(90deg,${C.teal} 0 8px,transparent 8px 14px)`,
                      }}
                    />
                  )}
                </div>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                    paddingRight: 24,
                  }}
                >
                  <div
                    style={{ display: "flex", gap: 10, alignItems: "center" }}
                  >
                    <span
                      style={{
                        fontSize: 12,
                        fontWeight: 700,
                        letterSpacing: ".12em",
                        color: C.teal,
                        textTransform: "uppercase",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {s.who}
                    </span>
                    <span
                      style={{
                        width: 4,
                        height: 4,
                        borderRadius: 2,
                        background: "#b8c6c3",
                      }}
                    />
                    <span
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: C.muted,
                        whiteSpace: "nowrap",
                      }}
                    >
                      {s.time}
                    </span>
                  </div>
                  <h3
                    style={{
                      margin: 0,
                      fontSize: 24,
                      fontWeight: 800,
                      letterSpacing: "-.02em",
                    }}
                  >
                    {s.title}
                  </h3>
                  <p
                    style={{
                      margin: 0,
                      fontSize: 15,
                      lineHeight: 1.7,
                      color: C.muted,
                      maxWidth: 320,
                    }}
                  >
                    {s.text}
                  </p>
                </div>
              </Reveal>
            );
          })}
        </div>

        <Reveal
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 20,
            flexWrap: "wrap",
            padding: "22px 28px",
            borderRadius: 20,
            background: "#fff",
            border: `1px solid ${C.line}`,
          }}
        >
          <span style={{ fontSize: 16 }}>
            <b>Ready in under a minute.</b>{" "}
            <span style={{ color: C.muted }}>Free, and no account needed.</span>
          </span>
          <a
            href="#plan"
            style={{
              padding: "13px 20px",
              borderRadius: 12,
              background: C.teal,
              color: "#fff",
              fontSize: 15,
              fontWeight: 700,
              whiteSpace: "nowrap",
              textDecoration: "none",
            }}
          >
            Start with your budget ↑
          </a>
        </Reveal>
      </div>
    </section>
  );
}

/* ---------------- Why us ---------------- */

function WhyUs() {
  const check: React.CSSProperties = {
    width: 18,
    height: 18,
    borderRadius: 9,
    background: "#fff",
    color: C.teal,
    display: "grid",
    placeItems: "center",
    fontSize: 10,
    fontWeight: 800,
    flex: "none",
  };
  return (
    <section id="why" style={{ background: "#e4e4e4", scrollMarginTop: 60 }}>
      <div
        style={{
          ...WRAP,
          padding: "112px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 56,
        }}
      >
        <Reveal
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,360px),1fr))",
            gap: "24px 56px",
            alignItems: "end",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <span style={EYEBROW}>Why BudgetSwarm</span>
            <h2 style={H2}>Stop planning in fifteen tabs</h2>
          </div>
          <div style={{ display: "flex", gap: 36, flexWrap: "wrap" }}>
            {(
              [
                ["4h → 1m", "average planning time"],
                ["15 → 1", "tabs open"],
              ] as [string, string][]
            ).map(([n, l]) => (
              <div
                key={l}
                style={{ display: "flex", flexDirection: "column", gap: 4 }}
              >
                <span
                  style={{
                    fontSize: 44,
                    fontWeight: 800,
                    letterSpacing: "-.03em",
                    lineHeight: 1,
                    color: C.teal,
                  }}
                >
                  {n}
                </span>
                <span style={{ fontSize: 13, color: C.muted }}>{l}</span>
              </div>
            ))}
          </div>
        </Reveal>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,380px),1fr))",
            gap: 20,
            alignItems: "stretch",
          }}
        >
          {/* Planning yourself */}
          <Reveal
            style={{
              borderRadius: 26,
              background: "#b6d7cf",
              border: `1px solid ${C.line}`,
              padding: 28,
              display: "flex",
              flexDirection: "column",
              gap: 22,
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span
                style={{
                  fontSize: 13,
                  fontWeight: 700,
                  letterSpacing: ".06em",
                  color: C.grey,
                }}
              >
                PLANNING YOURSELF
              </span>
              <span style={{ fontSize: 12, color: C.grey }}>
                {TABS.length} tabs open
              </span>
            </div>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 6,
                padding: 16,
                borderRadius: 16,
                background: "#e9eeec",
                minHeight: 120,
                alignContent: "flex-start",
              }}
            >
              {TABS.map((t, i) => (
                <span
                  key={t}
                  style={{
                    padding: "6px 10px",
                    borderRadius: 8,
                    background: "#fff",
                    border: "1px solid #e1e7e5",
                    fontSize: 11,
                    color: "#6b7876",
                    whiteSpace: "nowrap",
                    transform: `rotate(${TILT[i]}deg)`,
                    boxShadow: "0 2px 4px rgba(0,0,0,.04)",
                  }}
                >
                  {t} ✕
                </span>
              ))}
            </div>
            <div>
              {COMPARE.map(([l, a]) => (
                <div
                  key={l}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    padding: "12px 0",
                    borderTop: "1px dashed #e1e7e5",
                    fontSize: 14,
                    color: C.grey,
                  }}
                >
                  <span>{l}</span>
                  <span style={{ fontWeight: 600, textAlign: "right" }}>
                    {a}
                  </span>
                </div>
              ))}
            </div>
          </Reveal>

          {/* With BudgetSwarm */}
          <Reveal
            delay={120}
            style={{
              borderRadius: 26,
              background: C.teal,
              color: "#fff",
              padding: 28,
              display: "flex",
              flexDirection: "column",
              gap: 22,
              boxShadow: "0 30px 60px -30px rgba(15,80,72,.55)",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span
                style={{
                  fontSize: 13,
                  fontWeight: 800,
                  letterSpacing: ".06em",
                }}
              >
                WITH BUDGETSWARM
              </span>
              <span style={{ fontSize: 12, color: "#c9ece6" }}>1 tab</span>
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 10,
                padding: 16,
                borderRadius: 16,
                background: "rgba(255,255,255,.1)",
                minHeight: 120,
                justifyContent: "center",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "12px 14px",
                  borderRadius: 12,
                  background: "#fff",
                  color: C.ink,
                }}
              >
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 2 }}
                >
                  <b style={{ fontSize: 15 }}>Istanbul, 5 days</b>
                  <span style={{ fontSize: 12, color: C.muted }}>
                    Flights, stay and 20 stops
                  </span>
                </div>
                <b style={{ fontSize: 18 }}>€329</b>
              </div>
              <span
                style={{ fontSize: 12, color: "#c9ece6", textAlign: "center" }}
              >
                Everything in one plan, ready to book
              </span>
            </div>
            <div>
              {COMPARE.map(([l, , b]) => (
                <div
                  key={l}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    padding: "12px 0",
                    borderTop: "1px solid rgba(255,255,255,.14)",
                    fontSize: 14,
                  }}
                >
                  <span style={{ color: "#c9ece6" }}>{l}</span>
                  <span
                    style={{
                      display: "flex",
                      gap: 8,
                      alignItems: "center",
                      fontWeight: 700,
                      textAlign: "right",
                    }}
                  >
                    <span style={check}>✓</span>
                    {b}
                  </span>
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

/* ---------------- FAQ ---------------- */

function Faq() {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <section id="faq" style={{ background: "#dff0ec", scrollMarginTop: 60 }}>
      <div
        style={{
          ...WRAP,
          padding: "104px 32px",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,320px),1fr))",
          gap: 48,
          alignItems: "start",
        }}
      >
        <Reveal style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <span style={EYEBROW}>FAQ</span>
            <h2 style={{ ...H2, fontSize: "clamp(32px,4.2vw,46px)" }}>
              Questions, answered
            </h2>
            <p
              style={{
                margin: 0,
                fontSize: 16,
                color: "#4a5856",
                lineHeight: 1.6,
                maxWidth: 360,
              }}
            >
              Everything you need to know before you plan your first trip.
            </p>
          </div>
          <div
            style={{
              marginTop: 8,
              padding: 22,
              borderRadius: 20,
              background: "#fff",
              border: "1.5px solid #b9ddd5",
              display: "flex",
              flexDirection: "column",
              gap: 12,
              maxWidth: 380,
            }}
          >
            <div style={{ display: "flex" }}>
              <span
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: 18,
                  background: C.teal,
                  color: "#fff",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 13,
                  fontWeight: 800,
                  border: "2px solid #fff",
                }}
              >
                AM
              </span>
              <span
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: 18,
                  background: "#2dd4bf",
                  color: "#062a26",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 13,
                  fontWeight: 800,
                  border: "2px solid #fff",
                  marginLeft: -10,
                }}
              >
                JK
              </span>
            </div>
            <b style={{ fontSize: 17 }}>Still have a question?</b>
            <span style={{ fontSize: 14, color: C.muted, lineHeight: 1.5 }}>
              Our team usually replies within a few hours.
            </span>
            <button
              type="button"
              style={{
                all: "unset",
                cursor: "pointer",
                alignSelf: "start",
                marginTop: 4,
                padding: "10px 16px",
                borderRadius: 11,
                background: C.teal,
                color: "#fff",
                fontSize: 14,
                fontWeight: 700,
              }}
            >
              Contact support →
            </button>
          </div>
        </Reveal>

        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {FAQS.map(([q, a, cat], i) => {
            const isOpen = open === i;
            return (
              <Reveal
                key={q}
                delay={i * 70}
                style={{
                  background: "#fff",
                  borderRadius: 18,
                  border: `1.5px solid ${isOpen ? C.teal : "transparent"}`,
                  boxShadow: isOpen
                    ? "0 16px 32px -20px rgba(15,80,72,.45)"
                    : "0 1px 2px rgba(15,80,72,.06)",
                  transition: "all .2s",
                }}
              >
                <button
                  type="button"
                  onClick={() => setOpen(isOpen ? null : i)}
                  aria-expanded={isOpen}
                  style={{
                    all: "unset",
                    boxSizing: "border-box",
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 16,
                    padding: "20px 22px",
                    cursor: "pointer",
                  }}
                >
                  <span
                    style={{
                      flex: "none",
                      width: 32,
                      fontSize: 13,
                      fontWeight: 800,
                      color: isOpen ? C.teal : "#9fb3af",
                    }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span
                    style={{
                      flex: 1,
                      fontSize: 17,
                      fontWeight: 700,
                      letterSpacing: "-.01em",
                    }}
                  >
                    {q}
                  </span>
                  <span
                    style={{
                      flex: "none",
                      width: 32,
                      height: 32,
                      borderRadius: 16,
                      display: "grid",
                      placeItems: "center",
                      fontSize: 18,
                      background: isOpen ? C.teal : "#eef4f2",
                      color: isOpen ? "#fff" : C.ink,
                      transform: isOpen ? "rotate(45deg)" : "none",
                      transition: "all .2s",
                    }}
                  >
                    +
                  </span>
                </button>
                {isOpen && (
                  <div
                    style={{
                      padding: "0 22px 22px 70px",
                      display: "flex",
                      flexDirection: "column",
                      gap: 12,
                    }}
                  >
                    <p
                      style={{
                        margin: 0,
                        fontSize: 15,
                        lineHeight: 1.7,
                        color: "#4a5856",
                        maxWidth: 560,
                      }}
                    >
                      {a}
                    </p>
                    <span
                      style={{
                        alignSelf: "start",
                        padding: "3px 9px",
                        borderRadius: 7,
                        background: C.tealTint,
                        color: C.tealDark,
                        fontSize: 11,
                        fontWeight: 700,
                      }}
                    >
                      {cat}
                    </span>
                  </div>
                )}
              </Reveal>
            );
          })}
        </div>
      </div>
    </section>
  );
}

/* ---------------- Footer ---------------- */

function Footer() {
  const link: React.CSSProperties = {
    color: "#c3d3d0",
    textDecoration: "none",
  };
  return (
    <footer style={{ background: "#027a69", color: "#fff" }}>
      <div
        style={{
          ...WRAP,
          padding: "28px 32px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 20,
          flexWrap: "wrap",
        }}
      >
        <div>
          <div style={{ fontSize: 15, fontWeight: 700 }}>
            © 2026 BudgetSwarm
          </div>
          <div style={{ fontSize: 13, color: "#9fb3af" }}>
            AI-powered travel planning for smarter budgets.
          </div>
        </div>
        <nav
          style={{ display: "flex", gap: 24, fontSize: 14, flexWrap: "wrap" }}
        >
          <a href="#faq" style={link}>
            Contact support
          </a>
          <a href="#faq" style={link}>
            FAQ
          </a>
          <a href="#why" style={link}>
            Why us
          </a>
          <a href="#how" style={link}>
            How it works
          </a>
        </nav>
      </div>
    </footer>
  );
}
