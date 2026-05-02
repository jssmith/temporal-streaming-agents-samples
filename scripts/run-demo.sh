#!/usr/bin/env bash
#
# Bring up the streaming-agents demo stack.
#
# Usage:
#   scripts/run-demo.sh analytics    # backend-temporal worker + BFF + frontend
#   scripts/run-demo.sh voice        # voice-terminal worker; client runs in another terminal
#
# Idempotent re: Temporal: if the dev server is already reachable on
# localhost:7233 it attaches; otherwise it starts one in the background.
# Ctrl+C tears down only the processes this script started.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LOG_DIR="$ROOT/.run-demo"
mkdir -p "$LOG_DIR"

mode="${1:-}"
case "$mode" in
  analytics|voice) ;;
  *) echo "usage: $0 {analytics|voice}" >&2; exit 2 ;;
esac

PIDS=()
CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  echo
  echo "[run-demo] shutting down..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

start_bg() {
  # Run a backgrounded process; tag stdout/stderr with a prefix so
  # multiple processes' output is distinguishable in one terminal.
  local label="$1"; shift
  ( "$@" 2>&1 | sed -u "s/^/[$label] /" ) &
  PIDS+=("$!")
}

# ---- Prereqs --------------------------------------------------------------

command -v temporal >/dev/null || {
  echo "[run-demo] 'temporal' CLI not found. brew install temporal (macOS)." >&2
  exit 1
}

# Decide which Temporal cluster the spawned processes will use, so we
# can (a) only boot a local dev server when actually needed, and
# (b) print where they connected to up front. Honors per-app .env via
# the same path python-dotenv would.
TARGET_ADDR=""
TARGET_NS=""
if [ "$mode" = "analytics" ] && [ -f apps/backend-temporal/.env ]; then
  while IFS='=' read -r key value; do
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    case "$key" in
      TEMPORAL_ADDRESS) TARGET_ADDR="$value" ;;
      TEMPORAL_NAMESPACE) TARGET_NS="$value" ;;
    esac
  done < <(grep -v "^[[:space:]]*#" apps/backend-temporal/.env | grep "=")
fi
# voice-terminal hardcodes localhost:7233 in worker.py / main_temporal.py.
[ "$mode" = "voice" ] && TARGET_ADDR="localhost:7233"
TARGET_ADDR="${TARGET_ADDR:-localhost:7233}"
case "$TARGET_ADDR" in
  localhost:*|127.0.0.1:*) USE_LOCAL_TEMPORAL=true ;;
  *) USE_LOCAL_TEMPORAL=false ;;
esac
command -v uv >/dev/null || {
  echo "[run-demo] 'uv' not found. https://docs.astral.sh/uv/" >&2
  exit 1
}
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "[run-demo] OPENAI_API_KEY not set. source ~/api_keys.sh or export it before running." >&2
  exit 1
fi
if [ ! -f "$ROOT/data/chinook.sqlite" ]; then
  echo "[run-demo] Chinook database missing; running setup.sh..."
  "$ROOT/setup.sh"
fi

# ---- Temporal dev server --------------------------------------------------

if [ "$USE_LOCAL_TEMPORAL" = true ]; then
  if temporal operator namespace list >/dev/null 2>&1; then
    echo "[temporal] already reachable on $TARGET_ADDR (using existing server)"
  else
    echo "[temporal] starting dev server (log: $LOG_DIR/temporal.log)..."
    ( temporal server start-dev > "$LOG_DIR/temporal.log" 2>&1 ) &
    PIDS+=("$!")
    for _ in $(seq 1 60); do
      if temporal operator namespace list >/dev/null 2>&1; then
        echo "[temporal] ready (UI: http://localhost:8233)"
        break
      fi
      sleep 0.5
    done
    if ! temporal operator namespace list >/dev/null 2>&1; then
      echo "[temporal] failed to start; tail of $LOG_DIR/temporal.log:" >&2
      tail -n 20 "$LOG_DIR/temporal.log" >&2 || true
      exit 1
    fi
  fi
else
  echo "[temporal] target is remote ($TARGET_ADDR); not starting a local dev server"
fi

# ---- Workspace deps -------------------------------------------------------

# uv sync at the workspace root installs all apps + packages/shared in one
# shot. Cheap if already up to date.
( cd "$ROOT" && uv sync --quiet --all-packages )

# ---- App ------------------------------------------------------------------

if [ "$mode" = "analytics" ]; then
  start_bg worker bash -c 'cd apps/backend-temporal && uv run python -m src.worker'
  start_bg bff    bash -c 'cd apps/backend-temporal && uv run uvicorn src.main:app --port 8001'

  if [ ! -d apps/frontend/node_modules ]; then
    echo "[frontend] installing npm dependencies (one-time, ~30s)..."
    ( cd apps/frontend && npm install --silent )
  fi
  start_bg frontend bash -c 'cd apps/frontend && npm run dev'

  echo
  echo "============================================================"
  echo "  Analytics demo: http://localhost:3001"
  echo "  Temporal:       $TARGET_ADDR ${TARGET_NS:+(namespace: $TARGET_NS) }$([ "$USE_LOCAL_TEMPORAL" = true ] && echo "[local dev]" || echo "[remote, per .env]")"
  [ "$USE_LOCAL_TEMPORAL" = true ] && echo "  Temporal UI:    http://localhost:8233"
  echo "  Press Ctrl+C to stop everything this script started."
  echo "============================================================"
  wait
fi

if [ "$mode" = "voice" ]; then
  start_bg worker bash -c 'cd apps/voice-terminal && uv run python -m src.worker'

  echo
  echo "============================================================"
  echo "  Voice worker running."
  echo "  Temporal:       $TARGET_ADDR [local dev, hardcoded in worker]"
  echo "  Temporal UI:    http://localhost:8233"
  echo "  In a second terminal, run the voice client:"
  echo "      cd apps/voice-terminal && uv run python -m src.main_temporal"
  echo "  Press Ctrl+C here to stop the worker."
  echo "============================================================"
  wait
fi
