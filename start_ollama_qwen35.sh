#!/bin/bash
# User-space Ollama daemon supervisor for qwen3.5:35b on port 11435.
# This pairs with system Ollama on 11434 (which has deepseek-r1:32b etc).
#
# Why we need this: 2026-05-15 → 2026-05-20 the qwen3.5 daemon died silently,
# DB still pointed at it (ollama_host=11435 / ollama_model=qwen3.5:35b),
# SerenityAlphaTrader generated 761 fake HOLD signals over 5 days. No alert.
# See feedback-just-execute-dont-ask.md and silent-bypass plan item #7.

LOG=/tmp/ollama_qwen35.log
PORT=11435
BIN=/data/qbao775/.local/ollama/bin/ollama
MODELS=/data/qbao775/.ollama-new
GPUS="5,6"

echo "[$(date)] qwen3.5:35b daemon supervisor started" >> "$LOG"

while true; do
    if ! ss -tln 2>/dev/null | grep -q ":$PORT "; then
        echo "[$(date)] port $PORT not listening, starting daemon" >> "$LOG"
        CUDA_VISIBLE_DEVICES=$GPUS \
        OLLAMA_MODELS=$MODELS \
        OLLAMA_HOST=127.0.0.1:$PORT \
        "$BIN" serve >> "$LOG" 2>&1 &
        sleep 8
    fi

    # Probe health every 60s — daemon may be alive but model load broken
    health=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/tags" 2>/dev/null)
    if [ "$health" != "200" ]; then
        echo "[$(date)] health check failed (HTTP $health), restarting daemon" >> "$LOG"
        pkill -u "$(whoami)" -f "ollama serve" 2>/dev/null
        sleep 3
    fi

    sleep 60
done
