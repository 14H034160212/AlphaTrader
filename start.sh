#!/bin/bash
# SerenityAlphaTrader 启动脚本（含自动重启保活）

BACKEND_DIR="/data/qbao775/AlphaTrader/backend"
LOG_FILE="/tmp/alphatrader.log"
PYTHON="/data/qbao775/miniconda3/envs/alphatrader/bin/python3"
export CUDA_VISIBLE_DEVICES=7   # Use GPU-7 (~52GB free), avoid GPU 0-6 used by RL training

# Load secrets from backend/.env (gitignored). Required: SECRET_KEY for JWT signing.
# To rotate: edit backend/.env, restart the supervisor, all users must re-login.
ENV_FILE="$BACKEND_DIR/.env"
if [ ! -r "$ENV_FILE" ]; then
    echo "[$(date)] FATAL: $ENV_FILE missing or unreadable; backend will refuse to start." >> "$LOG_FILE"
    exit 1
fi
set -a
. "$ENV_FILE"
set +a

echo "[$(date)] SerenityAlphaTrader 守护进程启动" >> "$LOG_FILE"

while true; do
    echo "[$(date)] 启动服务器..." >> "$LOG_FILE"
    cd "$BACKEND_DIR"
    "$PYTHON" -c "
import uvicorn
uvicorn.run('main:app', host='0.0.0.0', port=8888, reload=False)
" >> "$LOG_FILE" 2>&1

    EXIT_CODE=$?
    echo "[$(date)] 服务器退出，退出码=$EXIT_CODE，5秒后重启..." >> "$LOG_FILE"
    sleep 5
done
