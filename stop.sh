#!/bin/bash
# SerenityAlphaTrader 停止脚本
# This script gracefully stops the SerenityAlphaTrader backend and the auto-restart loop in start.sh

echo "Stopping SerenityAlphaTrader..."

# Kill the while loop wrapper script
PIDS=$(pgrep -f "start.sh")
if [ -n "$PIDS" ]; then
    echo "Killing background start.sh script(s)... (PID: $PIDS)"
    kill $PIDS
fi

# Kill the actual Python/uvicorn server
# 2026-07-03: was matching port 8000, but the server actually runs on 8888
# (start.sh / main.py both use port=8888) — this grep never matched anything,
# so stop.sh always printed "successfully stopped" while the engine kept running.
# Root cause of the CRDO (7-1) and VST (7-2) rogue-sell recurrences.
UVICORN_PIDS=$(pgrep -f "uvicorn.*main:app.*8888")
if [ -n "$UVICORN_PIDS" ]; then
    echo "Killing uvicorn server(s)... (PID: $UVICORN_PIDS)"
    kill $UVICORN_PIDS
fi

# Wait for process termination
sleep 2

# Force kill if still running
if pgrep -f "uvicorn.*main:app.*8888" > /dev/null; then
    echo "Force killing remaining uvicorn processes..."
    pkill -9 -f "uvicorn.*main:app.*8888"
fi

echo "SerenityAlphaTrader has been successfully stopped."
