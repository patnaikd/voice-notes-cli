/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Node globals without pulling in @types/node.
declare const process: { env: Record<string, string | undefined> };

/** Backend for the dev proxy; override with VOICE_NOTES_BACKEND (e.g. the mock server). */
const backend = process.env.VOICE_NOTES_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/voice_notes/web/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend, ws: true, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
  },
});
