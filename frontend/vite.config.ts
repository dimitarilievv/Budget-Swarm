/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    // When served behind the HTTPS proxy (Caddy on 443), the HMR websocket must
    // connect to the proxy port, not Vite's internal 5173. Set via compose; unset
    // for plain `npm run dev` on http://localhost:5173.
    hmr: process.env.VITE_HMR_CLIENT_PORT
      ? { clientPort: Number(process.env.VITE_HMR_CLIENT_PORT) }
      : undefined,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/setupTests.ts",
    css: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      // Measure the app source, excluding entrypoints, types and test scaffolding.
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/main.tsx",
        "src/setupTests.ts",
        "src/vite-env.d.ts",
        "src/**/*.d.ts",
      ],
    },
  },
});
