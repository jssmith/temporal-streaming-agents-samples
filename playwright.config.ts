import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 120_000,
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
