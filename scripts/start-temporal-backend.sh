#!/usr/bin/env bash
#
# Boot the Temporal-backed analytics backend: worker in the background,
# FastAPI BFF in the foreground. Used as a single Playwright webServer entry
# so SIGTERM from Playwright tears both processes down together.
set -euo pipefail

cd "$(dirname "$0")/.."
cd apps/backend-temporal

uv run python -m src.worker &
WORKER_PID=$!

# `exec` would replace this shell with uvicorn and skip the EXIT trap, leaving
# the worker orphaned. Stay as the parent and forward TERM/INT to both.
trap 'kill "$WORKER_PID" "$BFF_PID" 2>/dev/null || true; wait "$WORKER_PID" 2>/dev/null || true; wait "$BFF_PID" 2>/dev/null || true' EXIT INT TERM

uv run uvicorn src.main:app --port 8001 &
BFF_PID=$!

wait "$BFF_PID"
