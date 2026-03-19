/**
 * Playwright config for demo recording.
 *
 * Enables video capture at 1280x720 with no retries (we want one clean run).
 * Uses the same webServer setup as the main config.
 */

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/recording",
  timeout: 300_000,
  expect: {
    timeout: 60_000,
  },
  retries: 0,
  use: {
    baseURL: "http://localhost:3001",
    viewport: { width: 1280, height: 720 },
    video: {
      mode: "on",
      size: { width: 1280, height: 720 },
    },
    trace: "off",
  },
  outputDir: "test-results/recording",
  webServer: [
    {
      command: "cd backend-ephemeral && uv run uvicorn src.main:app --port 8000",
      port: 8000,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "cd frontend && BACKEND_URL=http://localhost:8000 npm run dev",
      port: 3001,
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
