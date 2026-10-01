import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the UI runs on :5173/ui/ and proxies API calls to the FastAPI server on :8000.
// In production, `npm run build` output is served by FastAPI itself at /ui/ (no proxy needed).
const API = "http://127.0.0.1:8000";
const apiPaths = [
  "health", "symbols", "risk-config", "strategies", "backtests", "fits", "scan",
  "signals", "orders", "exits", "positions", "account", "kill-switch",
];

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(apiPaths.map((p) => [`/${p}`, API])),
  },
});
