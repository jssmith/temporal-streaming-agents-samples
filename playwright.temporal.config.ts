import { defineConfig } from "@playwright/test";

// E2E config that exercises the Temporal-backed BFF (apps/backend-temporal)
// on port 8001, with the worker booted alongside via start-temporal-backend.sh.
// The frontend runs on 3001 (its only supported port) with BACKEND_URL
// pointed at the Temporal BFF.
//
// Targets a local Temporal dev server on 127.0.0.1:7233 — env overrides
// below take precedence over apps/backend-temporal/.env (which points at
// Cloud). Start one with: temporal server start-dev
const localTemporalEnv = {
  TEMPORAL_ADDRESS: "localhost:7233",
  TEMPORAL_NAMESPACE: "default",
  TEMPORAL_API_KEY: "",
};

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
      env: localTemporalEnv,
    },
    {
      command: "cd apps/frontend && BACKEND_URL=http://localhost:8001 npm run dev",
      port: 3001,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
