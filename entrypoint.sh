#!/usr/bin/env bash
# ============================================================
# entrypoint.sh — Self-healing process manager for Render
# Restarts Uvicorn automatically on crash, without ever
# letting Render see a "deploy failed" exit code.
# ============================================================

set -u  # treat unset vars as errors, but DO NOT use `set -e`
        # (we want to catch failures ourselves, not exit on them)

HOST="0.0.0.0"
PORT="${PORT:-10000}"          # Render injects $PORT — fall back to 10000 locally
APP_MODULE="src.main:app"
RESTART_DELAY=5
LOG_PREFIX="[entrypoint]"

# Optional: cap restarts to avoid infinite crash-loops burning resources.
# Set MAX_RESTARTS=0 to allow unlimited restarts (default).
MAX_RESTARTS="${MAX_RESTARTS:-0}"
restart_count=0

echo "$LOG_PREFIX Starting self-healing supervisor for ${APP_MODULE}"
echo "$LOG_PREFIX Host=${HOST} Port=${PORT}"

# Forward termination signals from Render to the child process cleanly
trap 'echo "$LOG_PREFIX Caught SIGTERM/SIGINT, forwarding to uvicorn (PID ${UVICORN_PID:-n/a})"; kill -TERM "${UVICORN_PID:-}" 2>/dev/null; exit 0' SIGTERM SIGINT

while true; do
    echo "$LOG_PREFIX Launching Uvicorn (attempt #$((restart_count + 1)))..."

    uvicorn "$APP_MODULE" \
        --host "$HOST" \
        --port "$PORT" \
        --workers 1 \
        --loop uvloop \
        --log-level info &

    UVICORN_PID=$!
    wait "$UVICORN_PID"
    EXIT_CODE=$?

    echo "$LOG_PREFIX Uvicorn exited with code ${EXIT_CODE} at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

    restart_count=$((restart_count + 1))

    if [ "$MAX_RESTARTS" -ne 0 ] && [ "$restart_count" -ge "$MAX_RESTARTS" ]; then
        echo "$LOG_PREFIX Reached MAX_RESTARTS=${MAX_RESTARTS}. Stopping supervisor."
        exit 1
    fi

    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "$LOG_PREFIX Clean exit (code 0) — assuming intentional shutdown. Not restarting."
        exit 0
    fi

    echo "$LOG_PREFIX Restarting in ${RESTART_DELAY}s... (total restarts so far: ${restart_count})"
    sleep "$RESTART_DELAY"
done
