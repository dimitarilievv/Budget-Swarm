/**
 * Typed auth API calls + token storage.
 *
 * Thin wrappers over the shared Axios client for the `/api/auth/*` endpoints (register,
 * login, refresh, logout, me), mirroring the backend contract in docs/api.md. Every
 * failure is normalized to a typed {@link ApiError}.
 *
 * Tokens are persisted client-side: the access token is sent as `Authorization: Bearer`
 * (wired by {@link AuthContext}), the refresh token exchanges for a new pair. "Remember me"
 * decides the backing store — `localStorage` (survives a browser restart) vs
 * `sessionStorage` (cleared when the tab closes). Only one store ever holds the tokens.
 */
import type { AxiosInstance } from "axios";

import apiClient from "@/api/axios";
import { ApiError, toApiError } from "@/api/errors";

export interface AuthUser {
  id: number;
  email: string;
  name: string | null;
  created_at: string;
  updated_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResult {
  user: AuthUser;
  tokens: TokenPair;
}

/** `POST /api/auth/register`. */
export async function register(
  payload: { email: string; password: string; name?: string | null },
  client: AxiosInstance = apiClient,
): Promise<AuthResult> {
  try {
    const { data } = await client.post<AuthResult>(
      "/api/auth/register",
      payload,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `POST /api/auth/login`. */
export async function login(
  payload: { email: string; password: string },
  client: AxiosInstance = apiClient,
): Promise<AuthResult> {
  try {
    const { data } = await client.post<AuthResult>("/api/auth/login", payload);
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `POST /api/auth/refresh` — exchange a refresh token for a new (rotated) pair. */
export async function refresh(
  refreshToken: string,
  client: AxiosInstance = apiClient,
): Promise<TokenPair> {
  try {
    const { data } = await client.post<TokenPair>("/api/auth/refresh", {
      refresh_token: refreshToken,
    });
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `POST /api/auth/logout` — revoke a refresh token (idempotent). */
export async function logout(
  refreshToken: string,
  client: AxiosInstance = apiClient,
): Promise<void> {
  try {
    await client.post("/api/auth/logout", { refresh_token: refreshToken });
  } catch (err) {
    throw toApiError(err);
  }
}

/** `GET /api/auth/me` — the currently authenticated user. */
export async function getMe(
  client: AxiosInstance = apiClient,
): Promise<AuthUser> {
  try {
    const { data } = await client.get<AuthUser>("/api/auth/me");
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/**
 * `POST /api/auth/change-password` — set a new password.
 *
 * On success every session is revoked server-side and a fresh pair is returned for this
 * client; the caller should persist those new tokens so it stays signed in.
 */
export async function changePassword(
  payload: { current_password: string; new_password: string },
  client: AxiosInstance = apiClient,
): Promise<AuthResult> {
  try {
    const { data } = await client.post<AuthResult>(
      "/api/auth/change-password",
      payload,
    );
    return data;
  } catch (err) {
    throw toApiError(err);
  }
}

/** `DELETE /api/auth/me` — permanently delete the account and all its data. */
export async function deleteAccount(
  client: AxiosInstance = apiClient,
): Promise<void> {
  try {
    await client.delete("/api/auth/me");
  } catch (err) {
    throw toApiError(err);
  }
}

// --- token storage -----------------------------------------------------------

const ACCESS_KEY = "bs.auth.access";
const REFRESH_KEY = "bs.auth.refresh";
// Records which store ("local" | "session") currently holds the tokens, so reads and
// clears target the right one regardless of the current "remember" choice.
const SCOPE_KEY = "bs.auth.scope";

type StoreScope = "local" | "session";

function safe<T>(fn: () => T, fallback: T): T {
  // Storage can throw (private mode, disabled cookies) — never let that crash the app.
  try {
    return fn();
  } catch {
    return fallback;
  }
}

function storeFor(scope: StoreScope): Storage {
  return scope === "local" ? window.localStorage : window.sessionStorage;
}

/** Return the persisted tokens (from whichever store holds them), or `null`. */
export function loadTokens(): { access: string; refresh: string } | null {
  return safe(() => {
    const scope = (window.localStorage.getItem(SCOPE_KEY) ??
      window.sessionStorage.getItem(SCOPE_KEY)) as StoreScope | null;
    if (!scope) return null;
    const store = storeFor(scope);
    const access = store.getItem(ACCESS_KEY);
    const refresh = store.getItem(REFRESH_KEY);
    return access && refresh ? { access, refresh } : null;
  }, null);
}

/** Persist a token pair, choosing the store by "remember me". Clears the other store. */
export function saveTokens(tokens: TokenPair, remember: boolean): void {
  safe(() => {
    const scope: StoreScope = remember ? "local" : "session";
    clearTokens(); // ensure only one store ever holds the tokens
    const store = storeFor(scope);
    store.setItem(ACCESS_KEY, tokens.access_token);
    store.setItem(REFRESH_KEY, tokens.refresh_token);
    store.setItem(SCOPE_KEY, scope);
  }, undefined);
}

/** Update just the access/refresh values in place (used after a rotation). */
export function updateTokens(tokens: TokenPair): void {
  safe(() => {
    const scope = (window.localStorage.getItem(SCOPE_KEY) ??
      window.sessionStorage.getItem(SCOPE_KEY)) as StoreScope | null;
    if (!scope) return;
    const store = storeFor(scope);
    store.setItem(ACCESS_KEY, tokens.access_token);
    store.setItem(REFRESH_KEY, tokens.refresh_token);
  }, undefined);
}

/** Remove any persisted tokens from both stores. */
export function clearTokens(): void {
  safe(() => {
    for (const store of [window.localStorage, window.sessionStorage]) {
      store.removeItem(ACCESS_KEY);
      store.removeItem(REFRESH_KEY);
      store.removeItem(SCOPE_KEY);
    }
  }, undefined);
}

export { ApiError };
