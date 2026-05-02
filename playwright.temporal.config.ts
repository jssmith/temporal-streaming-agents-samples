import { defineConfig } from "@playwright/test";

// E2E config that exercises the Temporal-backed BFF (backend-temporal) on
// port 8001, with the worker booted alongside via start-temporal-backend.sh.
// The frontend runs on 3001 (its only supported port) with BACKEND_URL
// pointed at the Temporal BFF.
//
// Requires a reachable Temporal cluster — backend-temporal/.env defaults to
// Temporal Cloud; a local dev server on 127.0.0.1:7233 also works if env vars
// are unset.
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 180_000,
  expect: {
    timeout: 30_000,
  },
  use: {
    baseURL: "http://localhost:3001",
    trace: "on-first-retry",
  },
  retries: 1,
  webServer: [
    {
      command: "bash scripts/start-temporal-backend.sh",
      port: 8001,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "cd frontend && BACKEND_URL=http://localhost:8001 npm run dev",
      port: 3001,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
