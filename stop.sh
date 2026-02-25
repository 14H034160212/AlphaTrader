#!/bin/bash
# AlphaTrader 停止脚本
# This script gracefully stops the AlphaTrader backend and the auto-restart loop in start.sh

echo "Stopping AlphaTrader..."

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

echo "AlphaTrader has been successfully stopped."
