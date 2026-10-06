/**
 * AuthPage — the single login / register screen (centred card on teal).
 *
 * Presentational: it owns form state, client-side validation, the segmented Log in / Sign
 * up control and the password-strength meter, and calls the injected `onLogin` /
 * `onRegister` handlers. App wires those to {@link AuthContext} and drives the `/login` ⇄
 * `/register` URL through `initialMode` + `onModeChange`.
 *
 * Built to the design spec in the ticket (teal gradient, faint pin pattern, white card,
 * Plus Jakarta Sans, teal #0f9488). Styling is inline and self-contained so the page reads
 * identically regardless of surrounding CSS.
 */
import {
  useEffect,
  useState,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";

const TEAL = "#0f9488";
const TEAL_DARK = "#0b7a70";
const INK = "#12201e";
const MUTED = "#5d6b69";
const FIELD = "#dbe5e2";
const PIN =
  "M24 39.75c-5.58-6.3-9-10.8-9-15.75a9 9 0 0 1 18 0c0 4.95-3.42 9.45-9 15.75z";

export type AuthMode = "login" | "register";

export interface LoginValues {
  email: string;
  password: string;
  remember: boolean;
}

export interface RegisterValues {
  name: string;
  email: string;
  password: string;
}

export interface AuthPageProps {
  initialMode?: AuthMode;
  onLogin?: (values: LoginValues) => void | Promise<void>;
  onRegister?: (values: RegisterValues) => void | Promise<void>;
  onForgotPassword?: () => void;
  /** Called when the user switches tabs, so the parent can update the URL. */
  onModeChange?: (mode: AuthMode) => void;
  /** Optional success/info banner shown above the form (e.g. "Account created — log in"). */
  notice?: string;
  homeHref?: string;
}

function LogoWhite({ size = 34 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      style={{ display: "block", flex: "none" }}
      aria-hidden="true"
    >
      <rect x="2" y="2" width="44" height="44" rx="13" fill="#fff" />
      <ellipse
        cx="16.62"
        cy="16.8"
        rx="4.95"
        ry="2.88"
        transform="rotate(-32 16.62 16.8)"
        fill={TEAL}
        opacity=".5"
      />
      <ellipse
        cx="31.38"
        cy="16.8"
        rx="4.95"
        ry="2.88"
        transform="rotate(32 31.38 16.8)"
        fill={TEAL}
        opacity=".5"
      />
      <path
        d="M20.85 16.35c-1-3-3-4.5-5-4.5M27.15 16.35c1-3 3-4.5 5-4.5"
        fill="none"
        stroke={TEAL}
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="15.85" cy="11.85" r="1.6" fill={TEAL} />
      <circle cx="32.15" cy="11.85" r="1.6" fill={TEAL} />
      <defs>
        <clipPath id="authLogoClip">
          <path d={PIN} />
        </clipPath>
      </defs>
      <path d={PIN} fill={TEAL} />
      <g clipPath="url(#authLogoClip)">
        <rect x="13" y="25.08" width="22" height="2.52" fill="#f59e0b" />
        <rect x="13" y="30.12" width="22" height="2.52" fill="#f59e0b" />
      </g>
      <circle cx="24" cy="20.22" r="2.16" fill="#fff" />
    </svg>
  );
}

function PinPattern() {
  return (
    <svg
      width="100%"
      height="100%"
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        opacity: 0.09,
        pointerEvents: "none",
      }}
    >
      <defs>
        <pattern
          id="authPins"
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
      <rect width="100%" height="100%" fill="url(#authPins)" />
    </svg>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "13px 14px",
  borderRadius: 12,
  border: `1px solid ${FIELD}`,
  fontSize: 15,
  background: "#fff",
  color: INK,
  width: "100%",
  boxSizing: "border-box",
  fontFamily: "inherit",
  outline: "none",
};
const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
};
const labelText: React.CSSProperties = { fontSize: 13, fontWeight: 600 };

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  right?: ReactNode;
  error?: string;
};

function Field({ label, right, error, style, ...props }: FieldProps) {
  const [focus, setFocus] = useState(false);
  return (
    <label style={labelStyle}>
      <span
        style={{
          display: "flex",
          justifyContent: "space-between",
          ...labelText,
        }}
      >
        <span>{label}</span>
        {right}
      </span>
      <input
        {...props}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{
          ...inputStyle,
          ...(style || {}),
          borderColor: error ? "#e05a3a" : focus ? TEAL : FIELD,
          boxShadow: focus ? "0 0 0 4px rgba(15,148,136,.12)" : "none",
        }}
      />
      {error && <span style={{ fontSize: 12, color: "#c2410c" }}>{error}</span>}
    </label>
  );
}

function Checkbox({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  children: ReactNode;
}) {
  return (
    <label
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        fontSize: 14,
        color: "#4a5856",
        lineHeight: 1.5,
        cursor: "pointer",
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        style={{ position: "absolute", opacity: 0, width: 0, height: 0 }}
      />
      <span
        aria-hidden="true"
        style={{
          flex: "none",
          width: 20,
          height: 20,
          marginTop: 1,
          borderRadius: 6,
          display: "grid",
          placeItems: "center",
          fontSize: 12,
          fontWeight: 800,
          color: "#fff",
          background: checked ? TEAL : "#fff",
          border: `1.5px solid ${checked ? TEAL : "#cfdcd9"}`,
        }}
      >
        {checked ? "✓" : ""}
      </span>
      <span>{children}</span>
    </label>
  );
}

function passwordScore(p: string): number {
  return [
    p.length >= 8,
    /[A-Z]/.test(p) && /[a-z]/.test(p),
    /\d/.test(p),
    /[^A-Za-z0-9]/.test(p),
  ].filter(Boolean).length;
}

type FieldErrors = {
  name?: string;
  email?: string;
  password?: string;
  agree?: string;
  form?: string;
};

export default function AuthPage({
  initialMode = "login",
  onLogin,
  onRegister,
  onForgotPassword,
  onModeChange,
  notice,
  homeHref = "/",
}: AuthPageProps) {
  const [mode, setMode] = useState<AuthMode>(initialMode);
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [show, setShow] = useState(false);
  const [remember, setRemember] = useState(true);
  const [agree, setAgree] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [loading, setLoading] = useState(false);
  const isReg = mode === "register";

  // Keep the tab in sync with the URL-driven mode (browser back/forward).
  useEffect(() => setMode(initialMode), [initialMode]);

  const set =
    (k: "name" | "email" | "password") =>
    (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((f) => ({ ...f, [k]: e.target.value }));

  const score = passwordScore(form.password);
  const strengthColor = score <= 1 ? "#e0a100" : score <= 2 ? "#f59e0b" : TEAL;
  const strengthText = !form.password
    ? "Use 8+ characters with a mix of letters, numbers and symbols."
    : ["Too weak", "Weak", "Okay", "Good", "Strong"][score] + " password";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const err: FieldErrors = {};
    if (isReg && !form.name.trim()) err.name = "Please enter your name";
    if (!/^\S+@\S+\.\S+$/.test(form.email))
      err.email = "Enter a valid email address";
    if (!form.password) err.password = "Enter your password";
    else if (isReg && form.password.length < 8)
      err.password = "Use at least 8 characters";
    if (isReg && !agree) err.agree = "Please accept the terms to continue";
    setErrors(err);
    if (Object.keys(err).length) return;
    setLoading(true);
    try {
      if (isReg)
        await onRegister?.({
          name: form.name,
          email: form.email,
          password: form.password,
        });
      else
        await onLogin?.({
          email: form.email,
          password: form.password,
          remember,
        });
    } catch (ex) {
      setErrors({
        form:
          ex instanceof Error
            ? ex.message
            : "Something went wrong. Please try again.",
      });
    } finally {
      setLoading(false);
    }
  };

  const switchMode = (m: AuthMode) => {
    setMode(m);
    setErrors({});
    onModeChange?.(m);
  };

  return (
    <div
      style={{
        position: "relative",
        overflow: "hidden",
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "36px 20px 28px",
        background:
          "radial-gradient(120% 90% at 50% 0%,#14a596 0%,#0f9488 45%,#0b7a70 100%)",
        fontFamily: "'Plus Jakarta Sans',system-ui,sans-serif",
        color: INK,
        boxSizing: "border-box",
      }}
    >
      <PinPattern />

      <a
        href={homeHref}
        style={{
          position: "relative",
          display: "flex",
          alignItems: "center",
          gap: 10,
          color: "#fff",
          textDecoration: "none",
        }}
      >
        <LogoWhite />
        <span
          style={{ fontWeight: 800, fontSize: 19, letterSpacing: "-.02em" }}
        >
          Budget<span style={{ color: "#b6f2e8" }}>Swarm</span>
        </span>
      </a>

      <div
        style={{
          position: "relative",
          flex: 1,
          width: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "36px 0",
        }}
      >
        <form
          onSubmit={submit}
          noValidate
          style={{
            width: "100%",
            maxWidth: 560,
            background: "#fff",
            borderRadius: 28,
            padding: "clamp(28px,4.5vw,52px)",
            display: "flex",
            flexDirection: "column",
            gap: 22,
            boxShadow: "0 40px 80px -30px rgba(0,30,27,.55)",
            boxSizing: "border-box",
          }}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 8,
              textAlign: "center",
              alignItems: "center",
            }}
          >
            <h1
              style={{
                margin: 0,
                fontSize: "clamp(30px,3.6vw,38px)",
                letterSpacing: "-.035em",
                fontWeight: 800,
                lineHeight: 1.1,
              }}
            >
              {isReg ? "Create your account" : "Welcome back"}
            </h1>
            <p
              style={{
                margin: 0,
                fontSize: 15,
                color: MUTED,
                lineHeight: 1.55,
              }}
            >
              {isReg
                ? "Save trips, compare destinations and plan faster next time."
                : "Log in to see your saved trips and pick up where you left off."}
            </p>
          </div>

          <div
            role="tablist"
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 4,
              padding: 4,
              borderRadius: 14,
              background: "#e9f1ef",
            }}
          >
            {(
              [
                ["login", "Log in"],
                ["register", "Sign up"],
              ] as [AuthMode, string][]
            ).map(([k, l]) => {
              const on = mode === k;
              return (
                <button
                  key={k}
                  type="button"
                  role="tab"
                  aria-selected={on}
                  onClick={() => switchMode(k)}
                  style={{
                    border: "none",
                    fontFamily: "inherit",
                    textAlign: "center",
                    padding: "10px 0",
                    borderRadius: 11,
                    fontSize: 14,
                    fontWeight: 700,
                    cursor: "pointer",
                    background: on ? "#fff" : "transparent",
                    color: on ? INK : MUTED,
                    boxShadow: on ? "0 1px 3px rgba(0,0,0,.08)" : "none",
                  }}
                >
                  {l}
                </button>
              );
            })}
          </div>

          {notice && !errors.form && (
            <div
              role="status"
              style={{
                padding: "10px 14px",
                borderRadius: 12,
                background: "#e7f6ee",
                color: "#1f7a52",
                fontSize: 13,
              }}
            >
              {notice}
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {isReg && (
              <Field
                label="Full name"
                placeholder="Ana Markovska"
                autoComplete="name"
                value={form.name}
                onChange={set("name")}
                error={errors.name}
              />
            )}
            <Field
              label="Email"
              type="email"
              placeholder="you@example.com"
              autoComplete="email"
              value={form.email}
              onChange={set("email")}
              error={errors.email}
            />

            <label style={labelStyle}>
              <span
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  ...labelText,
                }}
              >
                <span>Password</span>
                {!isReg && (
                  <button
                    type="button"
                    onClick={onForgotPassword}
                    style={{
                      all: "unset",
                      cursor: "pointer",
                      fontSize: 12,
                      color: TEAL_DARK,
                      fontWeight: 600,
                    }}
                  >
                    Forgot password?
                  </button>
                )}
              </span>
              <div style={{ position: "relative" }}>
                <input
                  type={show ? "text" : "password"}
                  value={form.password}
                  onChange={set("password")}
                  autoComplete={isReg ? "new-password" : "current-password"}
                  placeholder={
                    isReg ? "At least 8 characters" : "Your password"
                  }
                  style={{
                    ...inputStyle,
                    paddingRight: 64,
                    borderColor: errors.password ? "#e05a3a" : FIELD,
                  }}
                />
                <button
                  type="button"
                  onClick={() => setShow((s) => !s)}
                  aria-label={show ? "Hide password" : "Show password"}
                  style={{
                    all: "unset",
                    position: "absolute",
                    right: 12,
                    top: "50%",
                    transform: "translateY(-50%)",
                    fontSize: 12,
                    fontWeight: 700,
                    color: TEAL_DARK,
                    cursor: "pointer",
                  }}
                >
                  {show ? "Hide" : "Show"}
                </button>
              </div>
              {errors.password && (
                <span style={{ fontSize: 12, color: "#c2410c" }}>
                  {errors.password}
                </span>
              )}
            </label>

            {isReg && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(4,1fr)",
                    gap: 4,
                  }}
                >
                  {[0, 1, 2, 3].map((i) => (
                    <span
                      key={i}
                      style={{
                        height: 4,
                        borderRadius: 2,
                        background: i < score ? strengthColor : "#e4ebe9",
                        transition: "background .2s",
                      }}
                    />
                  ))}
                </div>
                <span style={{ fontSize: 12, color: MUTED }}>
                  {strengthText}
                </span>
              </div>
            )}

            {!isReg && (
              <Checkbox checked={remember} onChange={setRemember}>
                Keep me signed in
              </Checkbox>
            )}
            {isReg && (
              <>
                <Checkbox checked={agree} onChange={setAgree}>
                  I agree to the{" "}
                  <a href="/terms" style={{ color: TEAL_DARK }}>
                    Terms
                  </a>{" "}
                  and{" "}
                  <a href="/privacy" style={{ color: TEAL_DARK }}>
                    Privacy policy
                  </a>
                </Checkbox>
                {errors.agree && (
                  <span
                    style={{ fontSize: 12, color: "#c2410c", marginTop: -8 }}
                  >
                    {errors.agree}
                  </span>
                )}
              </>
            )}
          </div>

          {errors.form && (
            <div
              role="alert"
              style={{
                padding: "10px 14px",
                borderRadius: 12,
                background: "#fff4ed",
                color: "#c2410c",
                fontSize: 13,
              }}
            >
              {errors.form}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            style={{
              border: "none",
              fontFamily: "inherit",
              display: "flex",
              justifyContent: "center",
              padding: 15,
              borderRadius: 14,
              background: loading ? "#5fb8ae" : TEAL,
              color: "#fff",
              fontSize: 15,
              fontWeight: 700,
              cursor: loading ? "default" : "pointer",
            }}
          >
            {loading ? "Please wait…" : isReg ? "Create account" : "Log in"}
          </button>

          <p
            style={{
              margin: 0,
              textAlign: "center",
              fontSize: 14,
              color: MUTED,
            }}
          >
            {isReg ? "Already have an account?" : "New to BudgetSwarm?"}{" "}
            <button
              type="button"
              onClick={() => switchMode(isReg ? "login" : "register")}
              style={{
                all: "unset",
                cursor: "pointer",
                color: TEAL_DARK,
                fontWeight: 700,
              }}
            >
              {isReg ? "Log in" : "Create an account"}
            </button>
          </p>
        </form>
      </div>

      <div
        style={{
          position: "relative",
          display: "flex",
          gap: 18,
          alignItems: "center",
          fontSize: 12,
          color: "#d6f3ee",
          flexWrap: "wrap",
          justifyContent: "center",
        }}
      >
        <span>© 2026 BudgetSwarm</span>
        <a href="/terms" style={{ color: "#fff", textDecoration: "none" }}>
          Terms
        </a>
        <a href="/privacy" style={{ color: "#fff", textDecoration: "none" }}>
          Privacy
        </a>
        <a href="/support" style={{ color: "#fff", textDecoration: "none" }}>
          Help
        </a>
      </div>
    </div>
  );
}
