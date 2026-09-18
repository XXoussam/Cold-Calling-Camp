#!/bin/bash
# Runs the LiveKit worker (handles calls) and the Airtable scheduler poller
# (dispatches scheduled campaign runs) as two processes in one container,
# sharing the same filesystem/disk and env vars. If either dies, the whole
# container exits so Render restarts both together — simple, no separate
# process supervisor needed at this scale.
set -e

uv run python coldcall_agent.py start &
WORKER_PID=$!

uv run python scheduler_poller.py &
POLLER_PID=$!

wait -n "$WORKER_PID" "$POLLER_PID"
EXIT_CODE=$?

echo "one of the processes exited (code $EXIT_CODE) — shutting down the other and exiting"
kill "$WORKER_PID" "$POLLER_PID" 2>/dev/null || true
exit "$EXIT_CODE"
