/**
 * ProfilePage — the signed-in user's account screen (`/profile`).
 *
 * Built to the pasted ProfilePage design (sticky header with an active "Profile" pill, a
 * teal hero with avatar + stat boxes, and a sidebar switching between Account, Security and
 * Delete-account panels), wired to our real auth/trips APIs.
 *
 * Adapted to what the backend actually supports: the stat boxes are derived from the saved
 * trips (`GET /api/trips`); "Save changes" edits the display name (`PATCH /api/auth/me`);
 * the password form posts to `POST /api/auth/change-password` (which already revokes every
 * other session); "Download my data" exports the account + trips as JSON client-side; and
 * delete calls `DELETE /api/auth/me`. Design controls with no backing — avatar upload, home
 * city / currency prefs, email verification, and the per-device "active sessions" list — are
 * intentionally omitted rather than wired to nothing.
 */
import { useEffect, useMemo, useState } from "react";

import { ApiError } from "@/api/errors";
import { listTrips, type SavedTrip } from "@/api/endpoints";
import { useAuth } from "@/contexts/AuthContext";
import { navigate } from "@/router";

const TEAL = "#0f9488";
const TEAL_DARK = "#0b7a70";
const INK = "#12201e";
const MUTED = "#5d6b69";
const PAGE = "#f3f7f6";
const RED = "#c2410c";
const PIN =
  "M24 39.75c-5.58-6.3-9-10.8-9-15.75a9 9 0 0 1 18 0c0 4.95-3.42 9.45-9 15.75z";

const card: React.CSSProperties = {
  background: "#fff",
  borderRadius: 24,
  padding: "clamp(24px,3vw,36px)",
  display: "flex",
  flexDirection: "column",
  gap: 24,
  boxShadow: "0 24px 48px -32px rgba(0,40,36,.4)",
};
const input: React.CSSProperties = {
  padding: "13px 14px",
  borderRadius: 12,
  border: "1px solid #dbe5e2",
  fontSize: 15,
  color: INK,
  width: "100%",
  boxSizing: "border-box",
  fontFamily: "inherit",
  outline: "none",
};
const label: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
};
const lt: React.CSSProperties = { fontSize: 13, fontWeight: 600 };
const h2: React.CSSProperties = {
  margin: 0,
  fontSize: 22,
  fontWeight: 800,
  letterSpacing: "-.02em",
};

const ICONS: Record<string, string> = {
  account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  security: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  danger:
    "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
};

function Icon({
  d,
  color,
  size = 16,
}: {
  d: string;
  color: string;
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={d} />
    </svg>
  );
}

/** "€2.6k" compact, or "€840". */
function euroCompact(value: number): string {
  if (value >= 1000) {
    const k = value / 1000;
    return `€${k.toFixed(k >= 10 ? 0 : 1)}k`;
  }
  return `€${Math.round(value)}`;
}

/** "7 Oct 2026" from an ISO timestamp, or "—". */
function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
}

/** Up to two initials from a name, else the email's first letter. */
function initialsFor(name: string | null, email: string): string {
  const source = (name ?? "").trim();
  if (source) {
    return source
      .split(/\s+/)
      .slice(0, 2)
      .map((p) => p[0]!.toUpperCase())
      .join("");
  }
  return (email[0] ?? "?").toUpperCase();
}

type Tab = "account" | "security" | "danger";
const TABS: [Tab, string, string][] = [
  ["account", "Account", "Name and email"],
  ["security", "Security", "Change your password"],
  ["danger", "Delete account", "Remove your data"],
];

export default function ProfilePage() {
  const { user, logout, changePassword, deleteAccount, updateProfile } =
    useAuth();
  const [tab, setTab] = useState<Tab>("account");
  const [trips, setTrips] = useState<SavedTrip[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listTrips()
      .then((t) => !cancelled && setTrips(t))
      .catch(() => !cancelled && setTrips([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const stats = useMemo(() => {
    const saved = trips?.length ?? 0;
    const reserved = trips?.filter((t) => t.status === "reserved").length ?? 0;
    const spend = (trips ?? []).reduce(
      (sum, t) => sum + (t.total_cost ?? 0),
      0,
    );
    return { saved, reserved, spend: euroCompact(spend) };
  }, [trips]);

  if (!user) return null; // route guard renders this only when authenticated

  const link: React.CSSProperties = {
    padding: "7px 14px",
    color: MUTED,
    background: "none",
    border: "none",
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: 13,
    fontWeight: 500,
    whiteSpace: "nowrap",
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        background: PAGE,
        color: INK,
      }}
    >
      {/* Header */}
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
          }}
        >
          <button
            type="button"
            onClick={() => navigate("/")}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              background: "none",
              border: "none",
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            <img
              src="/logo.svg"
              alt=""
              aria-hidden="true"
              width={32}
              height={32}
            />
            <span style={{ fontWeight: 700, fontSize: 17, color: INK }}>
              Budget<span style={{ color: TEAL }}>Swarm</span>
            </span>
          </button>
          <nav style={{ display: "flex", gap: 4 }}>
            <button type="button" style={link} onClick={() => navigate("/")}>
              Plan trip
            </button>
            <button
              type="button"
              style={link}
              onClick={() => navigate("/my-trips")}
            >
              My trips
            </button>
          </nav>
          <span
            aria-current="page"
            style={{
              padding: "7px 14px",
              borderRadius: 99,
              background: "#fff",
              color: INK,
              fontSize: 13,
              border: `1px solid ${TEAL}`,
              fontWeight: 600,
            }}
          >
            Profile
          </span>
        </div>
      </header>

      {/* Teal hero */}
      <section
        style={{
          position: "relative",
          overflow: "hidden",
          background:
            "radial-gradient(120% 120% at 50% 0%,#14a596 0%,#0f9488 45%,#0b7a70 100%)",
          color: "#fff",
          padding: "44px 0 130px",
        }}
      >
        <svg
          width="100%"
          height="100%"
          aria-hidden="true"
          style={{
            position: "absolute",
            inset: 0,
            opacity: 0.08,
            pointerEvents: "none",
          }}
        >
          <defs>
            <pattern
              id="pfPins"
              width="96"
              height="208"
              patternUnits="userSpaceOnUse"
            >
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
          <rect width="100%" height="100%" fill="url(#pfPins)" />
        </svg>
        <div
          style={{
            position: "relative",
            maxWidth: 1200,
            margin: "0 auto",
            padding: "0 32px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            gap: 28,
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 22 }}>
            <div
              aria-hidden="true"
              style={{
                width: 96,
                height: 96,
                borderRadius: 30,
                background: "#fff",
                color: TEAL_DARK,
                display: "grid",
                placeItems: "center",
                fontSize: 34,
                fontWeight: 800,
                letterSpacing: "-.03em",
                boxShadow: "0 18px 36px -18px rgba(0,30,27,.6)",
              }}
            >
              {initialsFor(user.name, user.email)}
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                minWidth: 0,
              }}
            >
              <h1
                style={{
                  margin: 0,
                  fontSize: "clamp(32px,4vw,46px)",
                  letterSpacing: "-.04em",
                  fontWeight: 800,
                  lineHeight: 1,
                }}
              >
                {user.name || "Your account"}
              </h1>
              <span style={{ fontSize: 15, color: "#d6f3ee" }}>
                {user.email}
              </span>
              <span
                style={{
                  alignSelf: "start",
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  marginTop: 4,
                  padding: "5px 12px",
                  borderRadius: 99,
                  background: "rgba(255,255,255,.16)",
                  fontSize: 12,
                  fontWeight: 700,
                  whiteSpace: "nowrap",
                }}
              >
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#fff"
                  strokeWidth="2"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <rect x="3" y="5" width="18" height="16" rx="3" />
                  <path d="M3 10h18M8 3v4M16 3v4" />
                </svg>
                Member since {formatDate(user.created_at)}
              </span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            {(
              [
                [String(stats.saved), "Saved trips"],
                [String(stats.reserved), "Reserved"],
                [trips === null ? "—" : stats.spend, "Planned spend"],
              ] as [string, string][]
            ).map(([v, l]) => (
              <div
                key={l}
                style={{
                  padding: "14px 18px",
                  borderRadius: 16,
                  background: "rgba(255,255,255,.14)",
                  border: "1px solid rgba(255,255,255,.18)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 2,
                  minWidth: 96,
                }}
              >
                <span
                  style={{
                    fontSize: 26,
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
          </div>
        </div>
      </section>

      {/* Body */}
      <main
        style={{
          position: "relative",
          flex: 1,
          maxWidth: 1200,
          width: "100%",
          margin: "-90px auto 0",
          padding: "0 32px 80px",
          display: "flex",
          flexWrap: "wrap",
          gap: 24,
          alignItems: "flex-start",
          boxSizing: "border-box",
        }}
      >
        <aside
          style={{
            background: "#fff",
            borderRadius: 22,
            padding: 10,
            display: "flex",
            flexDirection: "column",
            gap: 2,
            boxShadow: "0 24px 48px -32px rgba(0,40,36,.4)",
            flex: "1 1 260px",
            maxWidth: 300,
          }}
        >
          {TABS.map(([k, l, sub]) => {
            const on = tab === k;
            const red = k === "danger";
            const col = red ? RED : TEAL;
            return (
              <button
                key={k}
                type="button"
                onClick={() => setTab(k)}
                aria-current={on ? "page" : undefined}
                style={{
                  fontFamily: "inherit",
                  textAlign: "left",
                  display: "flex",
                  gap: 12,
                  alignItems: "center",
                  padding: "12px 14px",
                  borderRadius: 14,
                  cursor: "pointer",
                  background: on
                    ? red
                      ? "#fff4ed"
                      : "#f0faf8"
                    : "transparent",
                  color: red ? "#9a3412" : INK,
                  border: `1px solid ${on ? (red ? "#f6d5c8" : "#cfe9e4") : "transparent"}`,
                }}
              >
                <span
                  style={{
                    flex: "none",
                    width: 34,
                    height: 34,
                    borderRadius: 11,
                    display: "grid",
                    placeItems: "center",
                    background: on ? col : red ? "#fff1ea" : "#e6f5f2",
                  }}
                >
                  <Icon d={ICONS[k]!} color={on ? "#fff" : col} />
                </span>
                <span
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 1,
                    minWidth: 0,
                  }}
                >
                  <b style={{ fontSize: 14, whiteSpace: "nowrap" }}>{l}</b>
                  <span
                    style={{
                      fontSize: 12,
                      color: MUTED,
                      fontWeight: 500,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {sub}
                  </span>
                </span>
              </button>
            );
          })}
          <span
            style={{ height: 1, background: "#edf1f0", margin: "6px 10px" }}
          />
          <button
            type="button"
            onClick={() => logout()}
            style={{
              fontFamily: "inherit",
              border: "none",
              background: "transparent",
              display: "flex",
              gap: 12,
              alignItems: "center",
              padding: "12px 14px",
              borderRadius: 14,
              fontSize: 14,
              fontWeight: 700,
              color: MUTED,
              cursor: "pointer",
            }}
          >
            <span
              style={{
                width: 34,
                height: 34,
                borderRadius: 11,
                background: "#f1f5f4",
                display: "grid",
                placeItems: "center",
              }}
            >
              <Icon d={ICONS.logout!} color={MUTED} />
            </span>
            Log out
          </button>
        </aside>

        <div
          style={{
            flex: "999 1 480px",
            minWidth: 0,
            display: "flex",
            flexDirection: "column",
            gap: 20,
          }}
        >
          {tab === "account" && (
            <AccountSection
              name={user.name}
              email={user.email}
              onSave={updateProfile}
            />
          )}
          {tab === "security" && (
            <SecuritySection onChangePassword={changePassword} />
          )}
          {tab === "danger" && (
            <DangerSection
              tripCount={stats.saved}
              reservedCount={stats.reserved}
              onDelete={deleteAccount}
              trips={trips}
              user={user}
            />
          )}
        </div>
      </main>
    </div>
  );
}

/* ---------- Account ---------- */
function AccountSection({
  name,
  email,
  onSave,
}: {
  name: string | null;
  email: string;
  onSave: (name: string | null) => Promise<void>;
}) {
  const [value, setValue] = useState(name ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);
  const dirty = (value.trim() || null) !== (name ?? null);

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      await onSave(value.trim() || null);
      setMsg({ ok: true, t: "Saved." });
    } catch (err) {
      setMsg({
        ok: false,
        t: err instanceof ApiError ? err.message : "Something went wrong.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={card}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <h2 style={h2}>Account details</h2>
        <span style={{ fontSize: 14, color: MUTED }}>
          Your name appears on saved trips. Email is used to sign in and can't
          be changed here.
        </span>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,240px),1fr))",
          gap: 16,
        }}
      >
        <label style={label}>
          <span style={lt}>Full name</span>
          <input
            style={input}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="name"
            placeholder="Your name"
          />
        </label>
        <label style={label}>
          <span style={lt}>Email</span>
          <input
            style={{ ...input, background: "#f6f8f7", color: MUTED }}
            type="email"
            value={email}
            readOnly
            autoComplete="email"
          />
        </label>
      </div>
      {msg && (
        <div
          role={msg.ok ? "status" : "alert"}
          style={{
            padding: "10px 14px",
            borderRadius: 12,
            fontSize: 13,
            background: msg.ok ? "#e7f6ee" : "#fff4ed",
            color: msg.ok ? "#1f8a55" : RED,
          }}
        >
          {msg.t}
        </div>
      )}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: 10,
          paddingTop: 18,
          borderTop: "1px solid #edf1f0",
        }}
      >
        <button
          type="button"
          onClick={() => {
            setValue(name ?? "");
            setMsg(null);
          }}
          style={{
            padding: "12px 18px",
            borderRadius: 12,
            border: "1px solid #d7e2df",
            background: "#fff",
            fontSize: 14,
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={save}
          disabled={!dirty || busy}
          style={{
            padding: "12px 20px",
            borderRadius: 12,
            border: "none",
            background: !dirty || busy ? "#e4ebe9" : TEAL,
            color: !dirty || busy ? "#9aa8a5" : "#fff",
            fontSize: 14,
            fontWeight: 700,
            cursor: !dirty || busy ? "not-allowed" : "pointer",
            fontFamily: "inherit",
          }}
        >
          {busy ? "Saving…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}

/* ---------- Security ---------- */
function SecuritySection({
  onChangePassword,
}: {
  onChangePassword: (current: string, next: string) => Promise<void>;
}) {
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [cf, setCf] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);

  const rules: [string, boolean][] = [
    ["At least 8 characters", nw.length >= 8],
    ["Upper and lower case", /[A-Z]/.test(nw) && /[a-z]/.test(nw)],
    ["A number", /\d/.test(nw)],
    ["A symbol", /[^A-Za-z0-9]/.test(nw)],
  ];
  const score = rules.filter((r) => r[1]).length;
  const col = score <= 1 ? "#e0a100" : score <= 2 ? "#f59e0b" : TEAL;
  const match = cf.length > 0 && cf === nw;
  const differs = nw !== cur;
  const ok = Boolean(cur) && nw.length >= 8 && match && differs && !busy;
  const type = show ? "text" : "password";

  async function submit() {
    if (!ok) return;
    setBusy(true);
    setMsg(null);
    try {
      await onChangePassword(cur, nw);
      setCur("");
      setNw("");
      setCf("");
      setMsg({
        ok: true,
        t: "Password updated. Other devices were signed out.",
      });
    } catch (err) {
      setMsg({
        ok: false,
        t:
          err instanceof ApiError
            ? err.message
            : "Current password is incorrect.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={card}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <h2 style={h2}>Change password</h2>
        <span style={{ fontSize: 14, color: MUTED }}>
          Updating your password signs you out on every other device.
        </span>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,300px),1fr))",
          gap: 28,
          alignItems: "start",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <label style={label}>
            <span style={lt}>Current password</span>
            <input
              style={input}
              type={type}
              value={cur}
              onChange={(e) => setCur(e.target.value)}
              placeholder="Enter current password"
              autoComplete="current-password"
            />
          </label>
          <label style={label}>
            <span style={lt}>New password</span>
            <input
              style={input}
              type={type}
              value={nw}
              onChange={(e) => setNw(e.target.value)}
              placeholder="At least 8 characters"
              autoComplete="new-password"
            />
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(4,1fr)",
                gap: 4,
                marginTop: 4,
              }}
            >
              {[0, 1, 2, 3].map((i) => (
                <span
                  key={i}
                  style={{
                    height: 4,
                    borderRadius: 2,
                    background: i < score ? col : "#e4ebe9",
                  }}
                />
              ))}
            </div>
          </label>
          <label style={label}>
            <span
              style={{
                ...lt,
                display: "flex",
                justifyContent: "space-between",
              }}
            >
              <span>Confirm new password</span>
              {cf && (
                <span
                  style={{ fontSize: 12, color: match ? "#1f8a55" : "#8a5a00" }}
                >
                  {match ? "✓ Passwords match" : "Doesn’t match yet"}
                </span>
              )}
            </span>
            <input
              style={input}
              type={type}
              value={cf}
              onChange={(e) => setCf(e.target.value)}
              placeholder="Repeat new password"
              autoComplete="new-password"
            />
          </label>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              fontSize: 14,
              color: "#4a5856",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              checked={show}
              onChange={(e) => setShow(e.target.checked)}
            />
            Show passwords
          </label>
        </div>
        <div
          style={{
            padding: 20,
            borderRadius: 18,
            background: "#f4f8f7",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <b style={{ fontSize: 14 }}>Your new password needs</b>
          {rules.map(([l, v]) => (
            <div
              key={l}
              style={{
                display: "flex",
                gap: 10,
                alignItems: "center",
                fontSize: 14,
              }}
            >
              <span
                style={{
                  flex: "none",
                  width: 20,
                  height: 20,
                  borderRadius: 10,
                  display: "grid",
                  placeItems: "center",
                  fontSize: 11,
                  fontWeight: 800,
                  color: "#fff",
                  background: v ? TEAL : "#fff",
                  border: `1.5px solid ${v ? TEAL : "#cfdcd9"}`,
                }}
              >
                {v ? "✓" : ""}
              </span>
              <span
                style={{ color: v ? INK : MUTED, fontWeight: v ? 600 : 500 }}
              >
                {l}
              </span>
            </div>
          ))}
        </div>
      </div>
      {msg && (
        <div
          role={msg.ok ? "status" : "alert"}
          style={{
            padding: "10px 14px",
            borderRadius: 12,
            fontSize: 13,
            background: msg.ok ? "#e7f6ee" : "#fff4ed",
            color: msg.ok ? "#1f8a55" : RED,
          }}
        >
          {msg.t}
        </div>
      )}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          paddingTop: 18,
          borderTop: "1px solid #edf1f0",
        }}
      >
        <button
          type="button"
          disabled={!ok}
          onClick={submit}
          style={{
            padding: "13px 22px",
            borderRadius: 12,
            border: "none",
            fontFamily: "inherit",
            fontSize: 14,
            fontWeight: 700,
            cursor: ok ? "pointer" : "not-allowed",
            background: ok ? TEAL : "#e4ebe9",
            color: ok ? "#fff" : "#9aa8a5",
          }}
        >
          {busy ? "Updating…" : "Update password"}
        </button>
      </div>
    </div>
  );
}

/* ---------- Danger ---------- */
function DangerSection({
  tripCount,
  reservedCount,
  onDelete,
  trips,
  user,
}: {
  tripCount: number;
  reservedCount: number;
  onDelete: () => Promise<void>;
  trips: SavedTrip[] | null;
  user: { name: string | null; email: string; created_at: string };
}) {
  const [conf, setConf] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = conf === "DELETE" && !busy;

  function download() {
    const blob = new Blob(
      [JSON.stringify({ account: user, trips: trips ?? [] }, null, 2)],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "budgetswarm-data.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function remove() {
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await onDelete();
      navigate("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      setBusy(false);
    }
  }

  return (
    <div style={{ ...card, gap: 20, border: "1px solid #f6d5c8" }}>
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <span
          style={{
            flex: "none",
            width: 48,
            height: 48,
            borderRadius: 16,
            background: "#fff1ea",
            display: "grid",
            placeItems: "center",
          }}
        >
          <Icon d={ICONS.danger!} color={RED} size={22} />
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <h2 style={{ ...h2, color: "#9a3412" }}>Delete account</h2>
          <span style={{ fontSize: 14, color: MUTED, lineHeight: 1.55 }}>
            This permanently deletes your account and all {tripCount} saved
            trips. It cannot be undone.
          </span>
        </div>
      </div>
      <div
        style={{
          padding: "16px 18px",
          borderRadius: 14,
          background: "#fff7f3",
          display: "flex",
          flexDirection: "column",
          gap: 6,
          fontSize: 13,
          color: "#7c2d12",
          lineHeight: 1.5,
        }}
      >
        <b>You'll lose:</b>
        <span>
          • {tripCount} saved trips
          {reservedCount > 0 ? `, including ${reservedCount} reserved` : ""}
        </span>
        <span>• Your account and sign-in details</span>
      </div>
      <label style={label}>
        <span style={lt}>
          Type <b style={{ color: RED }}>DELETE</b> to confirm
        </span>
        <input
          style={{ ...input, maxWidth: 320, borderColor: "#f0cfc2" }}
          value={conf}
          onChange={(e) => setConf(e.target.value)}
          placeholder="DELETE"
          autoComplete="off"
        />
      </label>
      {error && (
        <p
          role="alert"
          style={{ margin: 0, fontSize: 13, fontWeight: 600, color: RED }}
        >
          {error}
        </p>
      )}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={download}
          style={{
            padding: "12px 18px",
            borderRadius: 12,
            border: "1px solid #d7e2df",
            background: "#fff",
            fontSize: 14,
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          Download my data
        </button>
        <button
          type="button"
          disabled={!ok}
          onClick={remove}
          style={{
            padding: "12px 18px",
            borderRadius: 12,
            border: "none",
            fontFamily: "inherit",
            fontSize: 14,
            fontWeight: 700,
            cursor: ok ? "pointer" : "not-allowed",
            background: ok ? RED : "#fbe3d9",
            color: ok ? "#fff" : "#c98a72",
          }}
        >
          {busy ? "Deleting…" : "Delete account"}
        </button>
      </div>
    </div>
  );
}
