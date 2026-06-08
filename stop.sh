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
UVICORN_PIDS=$(pgrep -f "uvicorn.*main:app.*8000")
if [ -n "$UVICORN_PIDS" ]; then
    echo "Killing uvicorn server(s)... (PID: $UVICORN_PIDS)"
    kill $UVICORN_PIDS
fi

# Wait for process termination
sleep 2

# Force kill if still running
if pgrep -f "uvicorn.*main:app.*8000" > /dev/null; then
    echo "Force killing remaining uvicorn processes..."
    pkill -9 -f "uvicorn.*main:app.*8000"
fi

echo "SerenityAlphaTrader has been successfully stopped."
