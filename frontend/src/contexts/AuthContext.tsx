/**
 * Authentication state for the app.
 *
 * Holds the current user and exposes `login` / `register` / `logout`, persisting the token
 * pair (see {@link module:api/auth}). It also wires the shared Axios client so that:
 *   - every request carries `Authorization: Bearer <access token>` when signed in, and
 *   - a `401` on a non-auth call triggers a single refresh-and-retry; if the refresh
 *     fails, the session is cleared.
 *
 * This makes the whole app auth-aware for free: once logged in, `generate-trip` is sent
 * authenticated (so the plan is saved and booking links are returned), and saved-trip
 * calls are authorized. Tokens live in web storage, never in React state, so the access
 * token is not kept in memory longer than a request needs it.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { AxiosError, AxiosRequestConfig } from "axios";

import apiClient from "@/api/axios";
import {
  clearTokens,
  getMe,
  loadTokens,
  login as loginRequest,
  logout as logoutRequest,
  refresh as refreshRequest,
  register as registerRequest,
  saveTokens,
  updateTokens,
  type AuthUser,
} from "@/api/auth";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

interface AuthContextValue {
  user: AuthUser | null;
  status: AuthStatus;
  login: (email: string, password: string, remember: boolean) => Promise<void>;
  register: (
    email: string,
    password: string,
    name?: string | null,
  ) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Auth endpoints must never trigger the refresh-retry loop. */
function isAuthPath(url: string | undefined): boolean {
  return Boolean(url && url.includes("/api/auth/"));
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>("loading");
  // Shared in-flight refresh so concurrent 401s wait on one request, not a stampede.
  const refreshInFlight = useRef<Promise<boolean> | null>(null);

  const endSession = useCallback(() => {
    clearTokens();
    setUser(null);
    setStatus("unauthenticated");
  }, []);

  // Attempt a token refresh using the stored refresh token. Returns whether it succeeded.
  const tryRefresh = useCallback(async (): Promise<boolean> => {
    if (refreshInFlight.current) return refreshInFlight.current;
    const run = (async () => {
      const tokens = loadTokens();
      if (!tokens) return false;
      try {
        const rotated = await refreshRequest(tokens.refresh);
        updateTokens(rotated);
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight.current = null;
      }
    })();
    refreshInFlight.current = run;
    return run;
  }, []);

  // Install the Authorization + refresh-on-401 interceptors for the app's lifetime.
  useEffect(() => {
    const reqId = apiClient.interceptors.request.use((config) => {
      const tokens = loadTokens();
      if (tokens) {
        config.headers = config.headers ?? {};
        config.headers.Authorization = `Bearer ${tokens.access}`;
      }
      return config;
    });

    const resId = apiClient.interceptors.response.use(
      (response) => response,
      async (error: AxiosError) => {
        const original = error.config as
          (AxiosRequestConfig & { _retry?: boolean }) | undefined;
        const status = error.response?.status;
        if (
          status === 401 &&
          original &&
          !original._retry &&
          !isAuthPath(original.url)
        ) {
          original._retry = true;
          const ok = await tryRefresh();
          if (ok) {
            const tokens = loadTokens();
            if (tokens) {
              original.headers = original.headers ?? {};
              (original.headers as Record<string, string>).Authorization =
                `Bearer ${tokens.access}`;
            }
            return apiClient(original);
          }
          endSession();
        }
        return Promise.reject(error);
      },
    );

    return () => {
      apiClient.interceptors.request.eject(reqId);
      apiClient.interceptors.response.eject(resId);
    };
  }, [tryRefresh, endSession]);

  // On first load, if we have tokens, resolve the current user (refreshing once if needed).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!loadTokens()) {
        setStatus("unauthenticated");
        return;
      }
      try {
        const me = await getMe();
        if (!cancelled) {
          setUser(me);
          setStatus("authenticated");
        }
      } catch {
        // The response interceptor already attempts a refresh; reaching here means the
        // session is unrecoverable.
        if (!cancelled) endSession();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [endSession]);

  const login = useCallback(
    async (email: string, password: string, remember: boolean) => {
      const result = await loginRequest({ email, password });
      saveTokens(result.tokens, remember);
      setUser(result.user);
      setStatus("authenticated");
    },
    [],
  );

  const register = useCallback(
    async (email: string, password: string, name?: string | null) => {
      // Create the account only — the user then signs in explicitly (register → login).
      // We intentionally discard the tokens the endpoint returns so registration never
      // auto-authenticates.
      await registerRequest({ email, password, name });
    },
    [],
  );

  const logout = useCallback(async () => {
    const tokens = loadTokens();
    if (tokens) {
      try {
        await logoutRequest(tokens.refresh);
      } catch {
        // Best-effort revoke; clear locally regardless.
      }
    }
    endSession();
  }, [endSession]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, status, login, register, logout }),
    [user, status, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Access the auth context; throws if used outside {@link AuthProvider}. */
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}

/**
 * Like {@link useAuth} but returns ``null`` outside a provider instead of throwing.
 * Use in presentational components that may be rendered standalone (e.g. in tests) and
 * only need to adapt to auth state when it's available.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}
