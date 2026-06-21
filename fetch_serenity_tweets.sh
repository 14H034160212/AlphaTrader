#!/usr/bin/env bash
#
# fetch_serenity_tweets.sh — pull Serenity's (@aleabitoreddit) LATEST tweets via
# Agent Reach's twitter CLI and save them where serenity_lens.py can read them.
#
# Solves the "yan-labs archive froze at 2026-06-08" problem: this fetches his
# live timeline directly. Uses the @BillSerenity burner account's cookies
# (never the user's main account) stored in ~/.agent-reach/twitter.env.
#
# Cron: a few times a day. Logs to /tmp/serenity_tweets.log.
set -uo pipefail
LOG=/tmp/serenity_tweets.log
exec >>"$LOG" 2>&1
echo "==== $(date -u +%FT%TZ) tweets fetch start ===="

OUT=/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit/data/serenity_latest_tweets.yaml
N=${1:-25}

source /data/qbao775/miniconda3/etc/profile.d/conda.sh
conda activate agentreach
export PATH="/data/qbao775/miniconda3/bin:$PATH"
# burner-account twitter auth (chmod 600)
source /home/qbao775/.agent-reach/twitter.env

TMP=$(mktemp) || exit 1
if timeout 120 twitter search "from:aleabitoreddit" -n "$N" 2>/dev/null \
      | grep -ivE "ExperimentalWarning|trace-warnings" > "$TMP" \
   && grep -q "ok: true" "$TMP"; then
    mv "$TMP" "$OUT"
    # Convert to the JSON schema serenity_lens._compute_recency_scores() expects
    # (createdAtISO / text / quotedTweet.text) so the lens can recency-score his
    # LIVE tweets — making the freshest tweet the recency anchor.
    JSON_OUT="${OUT%.yaml}.json"
    python3 - "$OUT" "$JSON_OUT" <<'PY'
import sys, yaml, json, datetime
src, dst = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(src)) or {}
out = []
for t in d.get("data", []):
    iso = ""
    raw = t.get("createdAt", "")
    try:  # "Sat Jun 20 03:52:23 +0000 2026" → ISO
        iso = datetime.datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except Exception:
        pass
    out.append({
        "createdAtISO": iso,
        "text": t.get("text", "") or "",
        "quotedTweet": {"text": ((t.get("quotedTweet") or {}).get("text", "")) or ""},
    })
json.dump(out, open(dst, "w"), ensure_ascii=False, indent=1)
print(f"JSON {len(out)} tweets → {dst}")
PY
    echo "OK saved $(grep -c '^- id:' "$OUT" 2>/dev/null || echo '?') tweets → $OUT"
else
    echo "FETCH_FAIL (auth expired? rate-limited?); keeping last-good"
    rm -f "$TMP"
fi
echo "==== $(date -u +%FT%TZ) tweets fetch done ===="
