#!/bin/bash
# AlphaTrader 启动脚本（含自动重启保活）

BACKEND_DIR="/data/home/qbao775/stock-trading-platform/backend"
LOG_FILE="/tmp/alphatrader.log"
PYTHON="python3.8"

echo "[$(date)] AlphaTrader 守护进程启动" >> "$LOG_FILE"

while true; do
    echo "[$(date)] 启动服务器..." >> "$LOG_FILE"
    cd "$BACKEND_DIR"
    "$PYTHON" -c "
import sys
sys.path.append('/home/qbao775/.local/lib/python3.8/site-packages')
import uvicorn
uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=False)
" >> "$LOG_FILE" 2>&1

    EXIT_CODE=$?
    echo "[$(date)] 服务器退出，退出码=$EXIT_CODE，5秒后重启..." >> "$LOG_FILE"
    sleep 5
done
