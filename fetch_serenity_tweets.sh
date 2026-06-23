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

# cron-robust: absolute paths, no conda-activate (which fails in cron's minimal env).
# `twitter` is a uv tool in ~/.local/bin; node + agentreach python from miniconda.
export PATH="/home/qbao775/.local/bin:/data/qbao775/miniconda3/bin:/usr/bin:/bin"
PY=/data/qbao775/miniconda3/envs/agentreach/bin/python
TWITTER=/home/qbao775/.local/bin/twitter
# burner-account twitter auth (chmod 600)
source /home/qbao775/.agent-reach/twitter.env

TMP=$(mktemp) || exit 1
if timeout 120 "$TWITTER" search "from:aleabitoreddit" -n "$N" 2>/dev/null \
      | grep -ivE "ExperimentalWarning|trace-warnings" > "$TMP" \
   && grep -q "ok: true" "$TMP"; then
    mv "$TMP" "$OUT"
    # Convert to the JSON schema serenity_lens._compute_recency_scores() expects
    # (createdAtISO / text / quotedTweet.text) so the lens can recency-score his
    # LIVE tweets — making the freshest tweet the recency anchor.
    JSON_OUT="${OUT%.yaml}.json"
    "$PY" - "$OUT" "$JSON_OUT" <<'PY'
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
    rm -f "$TMP"
    # FALLBACK: the twitter CLI broke (X changed their site, HTTP 404 on
    # ClientTransaction). Capture Serenity's CURRENT thinking via Exa instead —
    # news aggregators (Bitget/BlockBeats/TopicDigg) quote his tweets — and write
    # them as pseudo-tweets in the lens JSON schema so recency scoring keeps
    # tracking his latest focus. The YAML stays last-good.
    echo "FETCH_FAIL (twitter CLI down); trying Exa fallback for Serenity content"
    JSON_OUT="${OUT%.yaml}.json"
    MCP=/data/qbao775/miniconda3/bin/mcporter
    "$MCP" call exa.web_search_exa query="aleabitoreddit Serenity latest tweets CPO optics SIVE AAOI memory" numResults=8 2>/dev/null \
        | grep -ivE "ExperimentalWarning|trace-warnings" > "$TMP" || true
    "$PY" - "$TMP" "$JSON_OUT" <<'PY' || echo "Exa fallback parse failed; keeping last-good JSON"
import sys, re, json
raw = open(sys.argv[1]).read(); dst = sys.argv[2]
# split Exa result blocks on "Title:"; pull Published date + Highlights text
blocks = re.split(r"\nTitle:", raw)
out = []
for b in blocks:
    if "serenity" not in b.lower() and "aleabitoreddit" not in b.lower():
        continue
    dm = re.search(r"Published:\s*(\d{4}-\d{2}-\d{2})", b)
    iso = (dm.group(1) + "T00:00:00+00:00") if dm else ""
    # keep the body text (drop the URL/Published lines)
    txt = re.sub(r"(Published:|URL:|https?://)\S*", " ", b)
    txt = " ".join(txt.split())[:400]
    if len(txt) > 40:
        out.append({"createdAtISO": iso, "text": txt, "quotedTweet": {"text": ""}})
if out:
    json.dump(out, open(dst, "w"), ensure_ascii=False, indent=1)
    print(f"Exa fallback: {len(out)} Serenity items → {dst}")
else:
    raise SystemExit("no Serenity items parsed")
PY
    rm -f "$TMP"
fi
echo "==== $(date -u +%FT%TZ) tweets fetch done ===="
