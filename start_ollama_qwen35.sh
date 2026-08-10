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

# 2026-08-10: was hardcoded to GPU 6 ("all qbao775 jobs on GPUs 5,6"), but
# GPU 6 is now ~76GB/82GB consumed by a DIFFERENT user's (ntan607) vLLM job
# that's been running 9+ days -- not something this account can or should
# kill. Left with ~5GB free, Ollama couldn't load gemma4:31b and every
# generate call hung indefinitely (confirmed: 60s and 90s direct tests, zero
# response), while /api/tags kept answering fine the whole time. Pick
# whichever GPU has the most free memory at (re)start time instead of a
# fixed pin, so a future multi-day squeeze on any one GPU doesn't repeat this.
pick_gpu() {
    nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits 2>/dev/null \
        | awk -F',' '{gsub(/ /,"",$2); if ($2+0 > best) {best=$2+0; idx=$1}} END {print idx}'
}
GPUS=$(pick_gpu)
[ -z "$GPUS" ] && GPUS="6"  # fallback if nvidia-smi is unavailable

echo "[$(date)] qwen3.5:35b daemon supervisor started" >> "$LOG"

# 2026-08-10: /api/tags only checks that the HTTP server is up -- it says
# nothing about whether the model can actually generate. Found today via
# crossvalidate_satellite.py's 7th "same-type false positive" escalation
# ($0.88 paid Claude call each time) that the daemon was genuinely STUCK on
# /api/generate (tested directly: 60s then 90s, zero response both times)
# while /api/tags kept answering in ~2ms the whole time -- this supervisor's
# health check was blind to exactly the failure mode it exists to catch.
# Add a real generate probe every ~10 cycles (~10min) so a hung daemon gets
# force-restarted instead of silently costing paid escalations indefinitely.
GEN_PROBE_EVERY=10
cycle=0

while true; do
    if ! ss -tln 2>/dev/null | grep -q ":$PORT "; then
        GPUS=$(pick_gpu); [ -z "$GPUS" ] && GPUS="6"
        echo "[$(date)] port $PORT not listening, starting daemon on GPU $GPUS" >> "$LOG"
        CUDA_VISIBLE_DEVICES=$GPUS \
        OLLAMA_MODELS=$MODELS \
        OLLAMA_HOST=127.0.0.1:$PORT \
        "$BIN" serve >> "$LOG" 2>&1 &
        sleep 8
        cycle=0
    fi

    # Probe health every 60s — daemon may be alive but model load broken
    health=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/tags" 2>/dev/null)
    unhealthy=0
    if [ "$health" != "200" ]; then
        echo "[$(date)] /api/tags health check failed (HTTP $health), restarting daemon" >> "$LOG"
        unhealthy=1
    elif [ "$cycle" -ge "$GEN_PROBE_EVERY" ]; then
        cycle=0
        gen_code=$(curl -s -m 45 -o /dev/null -w "%{http_code}" \
            "http://127.0.0.1:$PORT/api/generate" \
            -d '{"model":"gemma4:31b","prompt":"reply OK","stream":false}' 2>/dev/null)
        if [ "$gen_code" != "200" ]; then
            echo "[$(date)] /api/generate probe failed (HTTP $gen_code after 45s) -- daemon is up but stuck, restarting" >> "$LOG"
            unhealthy=1
        fi
    fi

    if [ "$unhealthy" = "1" ]; then
        # Kill ONLY the process bound to $PORT (11435) — NOT every user
        # "ollama serve" (the old `pkill -f "ollama serve"` also took down the
        # 11434/11436/11438/11439 instances). Target just the PID on $PORT.
        bad_pid=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
        [ -n "$bad_pid" ] && kill "$bad_pid" 2>/dev/null
        sleep 3
        cycle=0
        continue
    fi

    cycle=$((cycle + 1))
    sleep 60
done
