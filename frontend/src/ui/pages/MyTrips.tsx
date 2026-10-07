/**
 * MyTripsPage — the signed-in user's saved trips (`/my-trips`), as boarding-pass tickets.
 *
 * Built to the design spec (teal hero, ticket cards with a torn stub, grid/list views,
 * search, delete-with-undo) and wired to the saved-trips API (`GET/PATCH/DELETE /api/trips`).
 *
 * Adapted to our data model: a saved trip is a plan header (origin, destination, mode,
 * budget, totals, duration) with **no travel dates or upcoming/past status**, so the
 * date-driven parts of the mock (the live "next trip" countdown and the
 * upcoming/draft/past grouping) are intentionally omitted — every saved plan is shown as a
 * ticket. "Open" loads the stored plan back into the results view. Codes shown on the
 * tickets are a 3-letter display label derived from the city, not real IATA codes.
 */
import { useEffect, useMemo, useState } from "react";

import { deleteTrip, getTrip, listTrips, updateTrip } from "@/api/endpoints";
import { useAuth } from "@/contexts/AuthContext";
import { useTripResult } from "@/hooks/useTripResult";
import { navigate } from "@/router";
import type { SavedTrip } from "@/api/endpoints";

const TEAL = "#0f9488";
const TEAL_DARK = "#0b7a70";
const INK = "#12201e";
const MUTED = "#5d6b69";
const PAGE = "#f3f7f6";
const PIN =
  "M24 39.75c-5.58-6.3-9-10.8-9-15.75a9 9 0 0 1 18 0c0 4.95-3.42 9.45-9 15.75z";
const LAB: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: ".14em",
  color: "#9aa8a5",
};

const barcode = (c: string) =>
  `repeating-linear-gradient(90deg,${c} 0 2px,transparent 2px 4px,${c} 4px 5px,transparent 5px 8px,${c} 8px 11px,transparent 11px 13px)`;

type Mode = "Plane" | "Car" | "Bus" | "Train";

function modeLabel(transportMode: string): Mode {
  switch (transportMode) {
    case "airplane":
      return "Plane";
    case "car":
      return "Car";
    case "bus":
      return "Bus";
    default:
      return "Train";
  }
}

/** A short 3-letter display code from a place name (not a real IATA code). */
function placeCode(name: string | null | undefined): string {
  const letters = (name ?? "").replace(/[^a-zA-Z]/g, "");
  return (letters.slice(0, 3) || "···").toUpperCase();
}

/** The plan's travel window ("22 Oct – 26 Oct 2026"), or a day count when no dates exist. */
function fmtDates(
  start: string | null,
  end: string | null,
  days: number,
): string {
  if (!start) return `${days} days`;
  const s = new Date(start);
  const sStr = s.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });
  if (!end) return sStr;
  const e = new Date(end);
  const eStr = e.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  return `${sStr} – ${eStr}`;
}

/* ---------- Icons ---------- */

function ModeIcon({
  mode,
  color = TEAL,
  size = 18,
}: {
  mode: Mode;
  color?: string;
  size?: number;
}) {
  const p = {
    fill: "none",
    stroke: color,
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-label={mode}>
      {mode === "Plane" && (
        <path
          d="M21 15.5v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0v5l-8 5v2l8-2.5V18l-2 1.5V21l3.5-1 3.5 1v-1.5L13 18v-5z"
          fill={color}
          transform="rotate(90 12 12)"
        />
      )}
      {mode === "Car" && (
        <g {...p}>
          <path d="M5 16V11l2-5h10l2 5v5M5 16h14M5 16v2M19 16v2M4 11h16" />
          <circle cx="8" cy="13.5" r=".8" fill={color} />
          <circle cx="16" cy="13.5" r=".8" fill={color} />
        </g>
      )}
      {mode === "Bus" && (
        <g {...p}>
          <rect x="5" y="3" width="14" height="15" rx="2" />
          <path d="M5 11h14M8 18v2M16 18v2" />
        </g>
      )}
      {mode === "Train" && (
        <g {...p}>
          <rect x="6" y="3" width="12" height="14" rx="3" />
          <path d="M6 10h12M9 21l1.5-4M15 21l-1.5-4" />
        </g>
      )}
    </svg>
  );
}

function Logo() {
  return (
    <svg
      width="32"
      height="32"
      viewBox="0 0 48 48"
      style={{ display: "block", flex: "none" }}
      aria-hidden="true"
    >
      <rect x="2" y="2" width="44" height="44" rx="13" fill={TEAL} />
      <ellipse
        cx="16.62"
        cy="16.8"
        rx="4.95"
        ry="2.88"
        transform="rotate(-32 16.62 16.8)"
        fill="#fff"
        opacity=".5"
      />
      <ellipse
        cx="31.38"
        cy="16.8"
        rx="4.95"
        ry="2.88"
        transform="rotate(32 31.38 16.8)"
        fill="#fff"
        opacity=".5"
      />
      <path
        d="M20.85 16.35c-1-3-3-4.5-5-4.5M27.15 16.35c1-3 3-4.5 5-4.5"
        fill="none"
        stroke="#fff"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="15.85" cy="11.85" r="1.6" fill="#fff" />
      <circle cx="32.15" cy="11.85" r="1.6" fill="#fff" />
      <defs>
        <clipPath id="mtLogo">
          <path d={PIN} />
        </clipPath>
      </defs>
      <path d={PIN} fill="#fff" />
      <g clipPath="url(#mtLogo)">
        <rect x="13" y="25.08" width="22" height="2.52" fill="#f59e0b" />
        <rect x="13" y="30.12" width="22" height="2.52" fill="#f59e0b" />
      </g>
      <circle cx="24" cy="20.22" r="2.16" fill={TEAL} />
    </svg>
  );
}

function PinPattern({ id, opacity = 0.08 }: { id: string; opacity?: number }) {
  return (
    <svg
      width="100%"
      height="100%"
      aria-hidden="true"
      style={{ position: "absolute", inset: 0, opacity, pointerEvents: "none" }}
    >
      <defs>
        <pattern id={id} width="96" height="208" patternUnits="userSpaceOnUse">
          <path
            transform="translate(-20 -30) scale(1.6)"
            d={PIN}
            fill="none"
            stroke="#fff"
            strokeWidth="1.2"
          />
          <path
            transform="translate(28 74) scale(1.6)"
            d={PIN}
            fill="none"
            stroke="#fff"
            strokeWidth="1.2"
          />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  );
}

/* ---------- Header ---------- */

function Header({
  userLabel,
  onLogout,
}: {
  userLabel: string;
  onLogout: () => void;
}) {
  const link: React.CSSProperties = {
    all: "unset",
    padding: "7px 14px",
    color: MUTED,
    cursor: "pointer",
    borderRadius: 9,
    whiteSpace: "nowrap",
    fontSize: 13,
    fontWeight: 500,
  };
  const active: React.CSSProperties = {
    ...link,
    background: "#fff",
    color: INK,
    boxShadow: "0 1px 2px rgba(0,0,0,.06)",
  };
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 30,
        background: "rgba(246,250,249,.85)",
        backdropFilter: "blur(10px)",
        borderBottom: "1px solid rgba(15,148,136,.1)",
      }}
    >
      <div
        style={{
          maxWidth: 1200,
          margin: "0 auto",
          padding: "14px 32px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 20,
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          onClick={() => navigate("/")}
          style={{
            all: "unset",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 10,
            color: INK,
          }}
        >
          <Logo />
          <span style={{ fontWeight: 700, fontSize: 17 }}>
            Budget<span style={{ color: TEAL }}>Swarm</span>
          </span>
        </button>
        <nav style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          <button type="button" style={link} onClick={() => navigate("/")}>
            Plan trip
          </button>
          <span aria-current="page" style={active}>
            My trips
          </span>
        </nav>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{ fontSize: 13, color: MUTED, whiteSpace: "nowrap" }}
            className="hidden sm:inline"
          >
            {userLabel}
          </span>
          <button
            type="button"
            onClick={onLogout}
            style={{
              all: "unset",
              padding: "7px 14px",
              borderRadius: 99,
              border: "1px solid #d7e2df",
              fontSize: 13,
              background: "#fff",
              color: INK,
              cursor: "pointer",
            }}
          >
            Log out
          </button>
        </div>
      </div>
    </header>
  );
}

/* ---------- Trip ticket ---------- */

function TripTicket({
  trip,
  onOpen,
  onRename,
  onDelete,
}: {
  trip: SavedTrip;
  onOpen: (t: SavedTrip) => void;
  onRename: (t: SavedTrip) => void;
  onDelete: (t: SavedTrip) => void;
}) {
  const [menu, setMenu] = useState(false);
  const mode = modeLabel(trip.transport_mode);
  const city = trip.destination_city ?? "Saved trip";
  const country = trip.destination_country ?? "";
  const total = trip.total_cost ?? 0;
  const under = Math.round(trip.budget - total);
  const label = trip.title || `${city}${country ? `, ${country}` : ""}`;

  const field = (name: string, value: React.ReactNode) => (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}
    >
      <span style={{ ...LAB, letterSpacing: ".12em" }}>{name}</span>
      <b
        style={{
          fontSize: 13,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {value}
      </b>
    </div>
  );

  return (
    <div
      className="mt-ticket"
      style={{
        position: "relative",
        background: "#fff",
        borderRadius: 22,
        boxShadow:
          "0 1px 2px rgba(15,80,72,.06),0 10px 24px -18px rgba(15,80,72,.35)",
      }}
    >
      <div
        className="mt-ticket-main"
        style={{
          position: "relative",
          padding: "20px 22px",
          display: "flex",
          flexDirection: "column",
          gap: 18,
          minWidth: 0,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              fontSize: 10,
              fontWeight: 800,
              letterSpacing: ".16em",
              color: "#9aa8a5",
              whiteSpace: "nowrap",
            }}
          >
            TRIP PASS · BS-{String(trip.id).padStart(4, "0").slice(-4)}
          </span>
          <span
            style={{
              padding: "3px 9px",
              borderRadius: 99,
              fontSize: 10,
              fontWeight: 800,
              letterSpacing: ".06em",
              textTransform: "uppercase",
              whiteSpace: "nowrap",
              background: trip.status === "reserved" ? TEAL : "#e6f5f2",
              color: trip.status === "reserved" ? "#fff" : TEAL_DARK,
            }}
          >
            {trip.status === "reserved" ? "🔒 Reserved" : "Saved"}
          </span>
          <button
            type="button"
            aria-label="Trip actions"
            onClick={() => setMenu((m) => !m)}
            style={{
              marginLeft: "auto",
              width: 30,
              height: 30,
              borderRadius: 15,
              border: "none",
              background: "#f1f5f4",
              cursor: "pointer",
              fontSize: 15,
              color: INK,
              flex: "none",
            }}
          >
            ⋯
          </button>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "auto minmax(40px,1fr) auto",
            gap: 14,
            alignItems: "center",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span
              style={{
                fontSize: 32,
                fontWeight: 800,
                letterSpacing: "-.03em",
                lineHeight: 1,
              }}
            >
              {placeCode(trip.origin_label)}
            </span>
            <span
              style={{
                fontSize: 12,
                color: MUTED,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                maxWidth: 120,
              }}
            >
              {trip.origin_label ?? "Origin"}
            </span>
          </div>
          <div
            style={{
              position: "relative",
              height: 30,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <span
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                top: "50%",
                borderTop: "2px dashed #b9ddd5",
              }}
            />
            <span
              style={{
                position: "absolute",
                left: -3,
                top: "50%",
                width: 8,
                height: 8,
                marginTop: -4,
                borderRadius: 4,
                background: "#b9ddd5",
              }}
            />
            <span
              style={{
                position: "absolute",
                right: -3,
                top: "50%",
                width: 8,
                height: 8,
                marginTop: -4,
                borderRadius: 4,
                background: TEAL,
              }}
            />
            <span
              style={{
                position: "relative",
                width: 34,
                height: 34,
                borderRadius: 17,
                background: "#e6f5f2",
                display: "grid",
                placeItems: "center",
                border: "3px solid #fff",
              }}
            >
              <ModeIcon mode={mode} />
            </span>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 2,
              alignItems: "flex-end",
              textAlign: "right",
            }}
          >
            <span
              style={{
                fontSize: 32,
                fontWeight: 800,
                letterSpacing: "-.03em",
                lineHeight: 1,
                color: TEAL,
              }}
            >
              {placeCode(trip.destination_city)}
            </span>
            <span
              style={{
                fontSize: 12,
                color: MUTED,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                maxWidth: 140,
              }}
            >
              {label}
            </span>
          </div>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(3,minmax(0,1fr))",
            gap: 10,
            paddingTop: 14,
            borderTop: "1px solid #edf1f0",
          }}
        >
          {field(
            "DATES",
            fmtDates(trip.start_date, trip.end_date, trip.duration_days),
          )}
          {field("TRAVELERS", trip.travelers)}
          {field("TRAVEL", mode)}
        </div>
      </div>

      {/* Stub */}
      <div
        className="mt-ticket-stub"
        style={{
          position: "relative",
          borderRadius: "0 22px 22px 0",
          background: TEAL,
          color: "#fff",
          padding: "20px 18px",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          gap: 14,
        }}
      >
        <span
          className="mt-notch mt-notch-tl"
          style={{
            position: "absolute",
            left: -12,
            top: -12,
            width: 24,
            height: 24,
            borderRadius: 12,
            background: PAGE,
          }}
        />
        <span
          className="mt-notch mt-notch-bl"
          style={{
            position: "absolute",
            left: -12,
            bottom: -12,
            width: 24,
            height: 24,
            borderRadius: 12,
            background: PAGE,
          }}
        />
        <span
          className="mt-tear"
          style={{
            position: "absolute",
            left: -1,
            top: 16,
            bottom: 16,
            borderLeft: "2px dashed rgba(255,255,255,.45)",
          }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span
            style={{
              fontSize: 10,
              fontWeight: 800,
              letterSpacing: ".14em",
              opacity: 0.8,
            }}
          >
            TOTAL
          </span>
          <span
            style={{
              fontSize: 28,
              fontWeight: 800,
              letterSpacing: "-.03em",
              lineHeight: 1.05,
            }}
          >
            €{Math.round(total)}
          </span>
          <span style={{ fontSize: 11, fontWeight: 600, opacity: 0.85 }}>
            {under >= 0
              ? `€${under} under €${Math.round(trip.budget)}`
              : `of €${Math.round(trip.budget)}`}
          </span>
        </div>
        <div
          style={{
            height: 34,
            borderRadius: 3,
            background: barcode("rgba(255,255,255,.85)"),
          }}
        />
        <button
          type="button"
          onClick={() => onOpen(trip)}
          style={{
            border: "none",
            fontFamily: "inherit",
            fontSize: 13,
            fontWeight: 700,
            padding: "9px 12px",
            borderRadius: 10,
            background: "#fff",
            color: TEAL_DARK,
            cursor: "pointer",
            whiteSpace: "nowrap",
          }}
        >
          Open →
        </button>
      </div>

      {menu && (
        <div
          role="menu"
          style={{
            position: "absolute",
            right: 14,
            top: 54,
            zIndex: 5,
            background: "#fff",
            borderRadius: 14,
            boxShadow: "0 18px 36px -12px rgba(0,0,0,.25)",
            border: "1px solid #e4ebe9",
            padding: 6,
            display: "flex",
            flexDirection: "column",
            minWidth: 170,
          }}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenu(false);
              onRename(trip);
            }}
            style={{
              all: "unset",
              padding: "9px 12px",
              borderRadius: 9,
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Rename
          </button>
          <span
            style={{ height: 1, background: "#edf1f0", margin: "4px 6px" }}
          />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenu(false);
              onDelete(trip);
            }}
            style={{
              all: "unset",
              padding: "9px 12px",
              borderRadius: 9,
              fontSize: 13,
              fontWeight: 600,
              color: "#c2410c",
              cursor: "pointer",
            }}
          >
            Delete trip
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------- Page ---------- */

export default function MyTripsPage() {
  const { user, logout } = useAuth();
  const { setResult } = useTripResult();
  const [trips, setTrips] = useState<SavedTrip[]>([]);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [q, setQ] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid");
  const [removed, setRemoved] = useState<SavedTrip | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listTrips();
        if (!cancelled) {
          setTrips(data);
          setLoadState("ready");
        }
      } catch {
        if (!cancelled) setLoadState("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // After the undo window, actually delete on the server.
  useEffect(() => {
    if (!removed) return;
    const pending = removed;
    const t = setTimeout(() => {
      deleteTrip(pending.id).catch(() => {
        // Re-surface the trip if the server delete failed.
        setTrips((ts) =>
          ts.some((x) => x.id === pending.id) ? ts : [pending, ...ts],
        );
      });
      setRemoved((r) => (r?.id === pending.id ? null : r));
    }, 6000);
    return () => clearTimeout(t);
  }, [removed]);

  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return trips.filter((t) =>
      `${t.destination_city ?? ""} ${t.destination_country ?? ""} ${t.title ?? ""}`
        .toLowerCase()
        .includes(needle),
    );
  }, [trips, q]);

  const uniqueDestinations = new Set(
    trips.map((t) => t.destination_city).filter(Boolean),
  ).size;
  const uniqueCountries = new Set(
    trips.map((t) => t.destination_country).filter(Boolean),
  ).size;

  const handleOpen = async (trip: SavedTrip) => {
    try {
      const detail = await getTrip(trip.id);
      // The snapshot carries the full plan (transport, stay, booking links, weather, dates);
      // mark it saved so the results view shows the "saved on …" notice + Refresh.
      setResult(detail.response, detail.request, {
        tripId: detail.trip_id,
        savedAt: detail.saved_at,
        status: detail.status,
        transportReserved: detail.transport_reserved,
        stayReserved: detail.stay_reserved,
      });
      navigate("/");
    } catch {
      // Leave the user on the page; a transient fetch error shouldn't lose their list.
    }
  };

  const handleRename = async (trip: SavedTrip) => {
    const next = window.prompt(
      "Rename this trip",
      trip.title ?? trip.destination_city ?? "",
    );
    if (next == null) return;
    const title = next.trim() || null;
    try {
      const updated = await updateTrip(trip.id, { title });
      setTrips((ts) => ts.map((t) => (t.id === trip.id ? updated : t)));
    } catch {
      /* ignore — keep the old title */
    }
  };

  const handleDelete = (trip: SavedTrip) => {
    setTrips((ts) => ts.filter((t) => t.id !== trip.id));
    setRemoved(trip);
  };

  const undo = () => {
    if (removed) setTrips((ts) => [removed, ...ts]);
    setRemoved(null);
  };

  const userLabel = user?.name || user?.email || "traveler";
  const firstName = (user?.name || user?.email || "traveler").split(/[ @]/)[0];

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        background: PAGE,
        color: INK,
        fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif",
      }}
    >
      <Header userLabel={userLabel} onLogout={() => logout()} />

      {/* Teal hero */}
      <section
        style={{
          position: "relative",
          overflow: "hidden",
          background:
            "radial-gradient(120% 120% at 50% 0%,#14a596 0%,#0f9488 45%,#0b7a70 100%)",
          color: "#fff",
          padding: "12px 0 150px",
        }}
      >
        <PinPattern id="mtHeroPins" />
        <div
          style={{
            position: "relative",
            maxWidth: 1200,
            margin: "0 auto",
            padding: "40px 32px 0",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            gap: 24,
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: "#d6f3ee" }}>
              Welcome back, {firstName}
            </span>
            <h1
              style={{
                margin: 0,
                fontSize: "clamp(36px,4.6vw,54px)",
                letterSpacing: "-.04em",
                fontWeight: 800,
                lineHeight: 1,
              }}
            >
              My trips
            </h1>
          </div>
          <div
            style={{
              display: "flex",
              gap: 28,
              alignItems: "flex-end",
              flexWrap: "wrap",
            }}
          >
            {(
              [
                [trips.length, "Saved trips"],
                [uniqueDestinations, "Destinations"],
                [uniqueCountries, "Countries"],
              ] as [number, string][]
            ).map(([v, l]) => (
              <div
                key={l}
                style={{ display: "flex", flexDirection: "column", gap: 2 }}
              >
                <span
                  style={{
                    fontSize: 28,
                    fontWeight: 800,
                    letterSpacing: "-.02em",
                    lineHeight: 1,
                  }}
                >
                  {v}
                </span>
                <span
                  style={{
                    fontSize: 12,
                    color: "#d6f3ee",
                    fontWeight: 600,
                    whiteSpace: "nowrap",
                  }}
                >
                  {l}
                </span>
              </div>
            ))}
            <button
              type="button"
              onClick={() => navigate("/")}
              style={{
                border: "none",
                fontFamily: "inherit",
                padding: "13px 20px",
                borderRadius: 14,
                background: "#fff",
                color: TEAL_DARK,
                fontSize: 15,
                fontWeight: 800,
                cursor: "pointer",
                boxShadow: "0 12px 24px -12px rgba(0,30,27,.5)",
              }}
            >
              + Plan a new trip
            </button>
          </div>
        </div>
      </section>

      <main
        style={{
          position: "relative",
          flex: 1,
          maxWidth: 1200,
          width: "100%",
          margin: "-118px auto 0",
          padding: "0 32px 80px",
          display: "flex",
          flexDirection: "column",
          gap: 24,
          boxSizing: "border-box",
        }}
      >
        {/* Toolbar */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 16,
            flexWrap: "wrap",
            padding: 8,
            borderRadius: 18,
            background: "#fff",
            border: "1px solid #e4ebe9",
          }}
        >
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              padding: "0 6px",
            }}
          >
            <h3 style={{ margin: 0, fontSize: 16, fontWeight: 800 }}>
              All trips
            </h3>
            <span
              style={{
                fontSize: 12,
                fontWeight: 700,
                padding: "2px 9px",
                borderRadius: 7,
                background: "#eef4f2",
                color: "#4a5856",
              }}
            >
              {trips.length}
            </span>
          </div>
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <div style={{ position: "relative" }}>
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search trips"
                aria-label="Search trips"
                style={{
                  width: 210,
                  padding: "10px 14px 10px 36px",
                  borderRadius: 12,
                  border: "1px solid #dbe5e2",
                  fontSize: 14,
                  background: "#f6faf9",
                  color: INK,
                  fontFamily: "inherit",
                  outline: "none",
                  boxSizing: "border-box",
                }}
              />
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#9aa8a5"
                strokeWidth="2"
                strokeLinecap="round"
                style={{
                  position: "absolute",
                  left: 12,
                  top: "50%",
                  transform: "translateY(-50%)",
                }}
              >
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-3.5-3.5" />
              </svg>
            </div>
            <div
              style={{
                display: "flex",
                gap: 2,
                padding: 3,
                background: "#f1f5f4",
                borderRadius: 11,
              }}
            >
              {(
                [
                  ["grid", "▦"],
                  ["list", "☰"],
                ] as [typeof view, string][]
              ).map(([k, icon]) => (
                <button
                  key={k}
                  type="button"
                  aria-label={`${k} view`}
                  aria-pressed={view === k}
                  onClick={() => setView(k)}
                  style={{
                    border: "none",
                    width: 34,
                    height: 32,
                    borderRadius: 8,
                    cursor: "pointer",
                    fontSize: 15,
                    background: view === k ? "#fff" : "transparent",
                    color: view === k ? TEAL : "#9aa8a5",
                    boxShadow:
                      view === k ? "0 1px 3px rgba(0,0,0,.08)" : "none",
                  }}
                >
                  {icon}
                </button>
              ))}
            </div>
          </div>
        </div>

        {loadState === "loading" && (
          <div style={{ padding: "64px 0", textAlign: "center", color: MUTED }}>
            Loading your trips…
          </div>
        )}

        {loadState === "error" && (
          <div
            style={{
              borderRadius: 24,
              background: "#fff",
              border: "1px solid #e4ebe9",
              padding: "48px 24px",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 14,
              textAlign: "center",
            }}
          >
            <b style={{ fontSize: 18 }}>Couldn't load your trips</b>
            <span style={{ fontSize: 14, color: MUTED }}>
              Check your connection and try again.
            </span>
            <button
              type="button"
              onClick={() => {
                setLoadState("loading");
                listTrips()
                  .then((d) => {
                    setTrips(d);
                    setLoadState("ready");
                  })
                  .catch(() => setLoadState("error"));
              }}
              style={{
                border: "none",
                fontFamily: "inherit",
                padding: "11px 18px",
                borderRadius: 12,
                background: TEAL,
                color: "#fff",
                fontSize: 14,
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              Retry
            </button>
          </div>
        )}

        {loadState === "ready" &&
          matches.length > 0 &&
          (view === "grid" ? (
            <div
              style={{
                display: "grid",
                gridTemplateColumns:
                  "repeat(auto-fill,minmax(min(100%,480px),1fr))",
                gap: 20,
              }}
            >
              {matches.map((t) => (
                <TripTicket
                  key={t.id}
                  trip={t}
                  onOpen={handleOpen}
                  onRename={handleRename}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          ) : (
            <div
              style={{
                background: "#fff",
                border: "1px solid #e4ebe9",
                borderRadius: 20,
                overflow: "hidden",
              }}
            >
              {matches.map((t) => {
                const total = t.total_cost ?? 0;
                return (
                  <div
                    key={t.id}
                    className="mt-list-row"
                    style={{
                      display: "grid",
                      gridTemplateColumns:
                        "56px minmax(0,1.4fr) minmax(0,1fr) auto",
                      gap: 18,
                      alignItems: "center",
                      padding: "14px 18px",
                      borderTop: "1px solid #edf1f0",
                    }}
                  >
                    <div
                      style={{
                        width: 56,
                        height: 56,
                        borderRadius: 14,
                        display: "grid",
                        placeItems: "center",
                        fontSize: 16,
                        fontWeight: 800,
                        color: TEAL_DARK,
                        background: "#e6f5f2",
                      }}
                    >
                      {placeCode(t.destination_city)}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                        minWidth: 0,
                      }}
                    >
                      <b
                        style={{
                          fontSize: 16,
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                      >
                        {t.title ||
                          `${t.destination_city ?? "Saved trip"}${t.destination_country ? `, ${t.destination_country}` : ""}`}
                      </b>
                      <span style={{ fontSize: 13, color: MUTED }}>
                        {fmtDates(t.start_date, t.end_date, t.duration_days)} ·{" "}
                        {t.travelers} travelers · {modeLabel(t.transport_mode)}
                      </span>
                    </div>
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                      }}
                    >
                      <b style={{ fontSize: 16 }}>€{Math.round(total)}</b>
                      <span style={{ fontSize: 12, color: MUTED }}>
                        of €{Math.round(t.budget)}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() => handleOpen(t)}
                      style={{
                        border: "none",
                        fontFamily: "inherit",
                        fontSize: 13,
                        fontWeight: 700,
                        padding: "9px 12px",
                        borderRadius: 10,
                        background: "#e6f5f2",
                        color: TEAL_DARK,
                        cursor: "pointer",
                        whiteSpace: "nowrap",
                      }}
                    >
                      Open →
                    </button>
                  </div>
                );
              })}
            </div>
          ))}

        {loadState === "ready" && matches.length === 0 && (
          <div
            style={{
              borderRadius: 24,
              background: "#fff",
              border: "1px solid #e4ebe9",
              padding: "64px 24px",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 14,
              textAlign: "center",
            }}
          >
            <svg width="72" height="72" viewBox="0 0 48 48">
              <rect x="2" y="2" width="44" height="44" rx="13" fill="#e6f5f2" />
              <path d={PIN} fill={TEAL} />
              <circle cx="24" cy="20.22" r="2.6" fill="#e6f5f2" />
            </svg>
            <b style={{ fontSize: 20 }}>
              {q ? `No trips match "${q}"` : "No saved trips yet"}
            </b>
            <span
              style={{
                fontSize: 15,
                color: MUTED,
                maxWidth: 380,
                lineHeight: 1.55,
              }}
            >
              {q
                ? "Try a different city or country, or clear the search."
                : "Generate a plan, then tap “Save trip” to keep it here."}
            </span>
            <button
              type="button"
              onClick={() => (q ? setQ("") : navigate("/"))}
              style={{
                border: "none",
                fontFamily: "inherit",
                marginTop: 6,
                padding: "11px 18px",
                borderRadius: 12,
                background: TEAL,
                color: "#fff",
                fontSize: 14,
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              {q ? "Clear search" : "Plan a trip"}
            </button>
          </div>
        )}
      </main>

      {removed && (
        <div
          role="status"
          style={{
            position: "fixed",
            left: "50%",
            bottom: 28,
            transform: "translateX(-50%)",
            zIndex: 40,
            display: "flex",
            alignItems: "center",
            gap: 18,
            padding: "12px 14px 12px 18px",
            borderRadius: 14,
            background: INK,
            color: "#fff",
            fontSize: 14,
            boxShadow: "0 18px 36px -12px rgba(0,0,0,.4)",
          }}
        >
          <span>{removed.destination_city ?? "Trip"} deleted</span>
          <button
            type="button"
            onClick={undo}
            style={{
              border: "none",
              fontFamily: "inherit",
              padding: "7px 12px",
              borderRadius: 9,
              background: "rgba(255,255,255,.12)",
              fontWeight: 700,
              cursor: "pointer",
              color: "#5eead4",
            }}
          >
            Undo
          </button>
        </div>
      )}

      <footer style={{ background: "#027a69", color: "#fff" }}>
        <div
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            padding: "24px 32px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 20,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 14, fontWeight: 700 }}>
            © 2026 BudgetSwarm
          </span>
          <nav
            style={{ display: "flex", gap: 22, fontSize: 13, flexWrap: "wrap" }}
          >
            <a
              href="/support"
              style={{ color: "#d6f3ee", textDecoration: "none" }}
            >
              Contact support
            </a>
            <a
              href="/terms"
              style={{ color: "#d6f3ee", textDecoration: "none" }}
            >
              Terms
            </a>
            <a
              href="/privacy"
              style={{ color: "#d6f3ee", textDecoration: "none" }}
            >
              Privacy
            </a>
          </nav>
        </div>
      </footer>
    </div>
  );
}
