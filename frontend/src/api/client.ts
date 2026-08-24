/**
 * Axios client (BS-016).
 * Base URL comes from the environment (VITE_API_BASE_URL), so the same build
 * points at local Docker, staging, or prod without code changes. Defaults to
 * the local backend for dev convenience.
 */
import axios from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export const apiClient = axios.create({
  baseURL,
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// Lightweight logging so failed requests are visible during development.
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    console.error("[api] request failed:", error?.message ?? error);
    return Promise.reject(error);
  },
);

export default apiClient;
