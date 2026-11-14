/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API serves the built assets from its own origin (ADR-010); the dev server proxies to it.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  server: { proxy: { "/api": "http://127.0.0.1:8787", "/health": "http://127.0.0.1:8787" } },
  test: { environment: "jsdom", include: ["tests/**/*.test.{ts,tsx}"] },
});
