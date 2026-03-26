/**
 * Playwright config for demo recording.
 *
 * Two projects run in parallel:
 *   - "app": records the frontend UI (demo.spec.ts)
 *   - "temporal": records the Temporal dev server UI (temporal.spec.ts)
 *
 * They synchronize via a shared file (tests/recording/sync.ts).
 * After the run, combine the videos side-by-side with FFmpeg.
 */

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/recording",
  timeout: 600_000,
  expect: {
    timeout: 60_000,
  },
  retries: 0,
  workers: 2, // run app + temporal in parallel
  fullyParallel: true,
  outputDir: "test-results/recording",
  projects: [
    {
      name: "app",
      testMatch: "demo.spec.ts",
      use: {
        baseURL: "http://localhost:3001",
        viewport: { width: 1280, height: 720 },
        video: {
          mode: "on",
          size: { width: 1280, height: 720 },
        },
        trace: "off",
      },
    },
    {
      name: "temporal",
      testMatch: "temporal.spec.ts",
      use: {
        baseURL: "http://localhost:8233",
        viewport: { width: 1280, height: 720 },
        video: {
          mode: "on",
          size: { width: 1280, height: 720 },
        },
        trace: "off",
      },
    },
  ],
  webServer: [
    {
      // Temporal worker — runs the agent workflows
      command: "cd backend-temporal && uv run python -m src.worker",
      port: 7233, // waits for Temporal server (must be started separately)
      timeout: 30_000,
      reuseExistingServer: true, // worker doesn't bind a port; just check Temporal is up
    },
    {
      // Temporal BFF — stateless proxy between frontend and Temporal
      command:
        "cd backend-temporal && uv run uvicorn src.main:app --port 8001",
      port: 8001,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      // Frontend — points to the temporal backend (port 8001 is the default)
      command: "cd frontend && npm run dev",
      port: 3001,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
