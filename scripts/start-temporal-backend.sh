#!/usr/bin/env bash
#
# Boot the Temporal-backed analytics backend: worker in the background,
# FastAPI BFF in the foreground. Used as a single Playwright webServer entry
# so SIGTERM from Playwright tears both processes down together.
set -euo pipefail

cd "$(dirname "$0")/.."
cd backend-temporal

uv run python -m src.worker &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM

exec uv run uvicorn src.main:app --port 8001
