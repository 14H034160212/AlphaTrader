#!/bin/bash
# SerenityAlphaTrader 启动脚本（含自动重启保活）

# 2026-07-03: 安全开关。此文件存在时拒绝启动 —— 防止旧的 auto_trade 引擎
# 与新的 Plan D + Serenity 卫星仓策略在同一个 Alpaca 账户上打架(已发生两次:
# 7-1 误卖 CRDO, 7-2 误卖 VST, 均为该引擎的 [AUTO-REBALANCE] 逻辑所为)。
# 要恢复旧系统,必须先删除这个文件 —— 这是一个刻意的、需要显式确认的动作。
#
# 退出码用 0 而不是 1: 本服务由 systemd (~/.config/systemd/user/alphatrader.service)
# 以 Restart=on-failure 管理,只在"非零退出/异常终止"时才会拉起。安全开关是主动关闭,
# 不是故障,退出码必须是 0,否则 systemd 会每 5 秒重新拉起 start.sh、被开关再次拒绝、
# 无限循环,把安全开关的"安静待机"意图变成日志刷屏(或触发 systemd 的 start-limit 进入
# failed 状态,届时仅删除本文件不足以恢复,还需 systemctl --user reset-failed alphatrader)。
KILL_SWITCH="/data/qbao775/AlphaTrader/.DISABLE_AUTOSTART"
if [ -f "$KILL_SWITCH" ]; then
    echo "[$(date)] 安全开关生效 ($KILL_SWITCH 存在) — 拒绝启动,退出。" >> /tmp/alphatrader.log
    echo "拒绝启动: $KILL_SWITCH 存在。删除该文件以恢复自动交易引擎。"
    exit 0
fi

BACKEND_DIR="/data/qbao775/AlphaTrader/backend"
LOG_FILE="/tmp/alphatrader.log"
PYTHON="/data/qbao775/miniconda3/envs/alphatrader/bin/python3"
export CUDA_VISIBLE_DEVICES=6   # Pinned to GPU-6 (shared with ollama+dp_m7); consolidated to GPUs 5,6 only

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
