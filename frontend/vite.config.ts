/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server forwards /api to Django, so the browser sees one origin and
// the backend needs no CORS configuration. Under docker compose the target is
// the backend service; on the host it is Django on port 8000.
const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The Host header is left as the browser sent it (localhost), which
    // Django's ALLOWED_HOSTS already accepts; rewriting it to "backend:8000"
    // would be rejected.
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
});
