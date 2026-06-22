#!/usr/bin/env bash
#
# fetch_smart_money.sh — track "smart money" + political trades as a SUPPLEMENTARY
# signal (user request 2026-06-21: watch Buffett/Berkshire, Pelosi, Trump, etc.).
#
# IMPORTANT positioning: this is awareness + cross-check ONLY. Serenity's CPO/AI
# chokepoint thesis stays the PRIMARY brain. These disclosures are LAGGED (13F
# +45d, STOCK Act +45d) — not a real-time edge — so they inform, never force a
# buy, and the "don't chase / buy at bottom" discipline still applies.
#
# Pulls via Agent Reach's Exa search, extracts tickers, flags overlap with
# Serenity's current focus, writes smart_money_signals.json for the engine/email.
# Cron: a couple times a week. Logs to /tmp/smart_money.log.
set -uo pipefail
LOG=/tmp/smart_money.log
exec >>"$LOG" 2>&1
echo "==== $(date -u +%FT%TZ) smart-money fetch start ===="

OUT=/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit/data/smart_money_signals.json
# cron-robust: absolute PATH (node+mcporter live in miniconda/bin); no conda-activate.
export PATH="/data/qbao775/miniconda3/bin:/usr/bin:/bin"
PY=/data/qbao775/miniconda3/envs/agentreach/bin/python
cd /data/qbao775/AlphaTrader  # mcporter reads exa config from ./config/mcporter.json

q() { timeout 90 mcporter call exa.web_search_exa query="$1" numResults=3 2>/dev/null | grep -ivE "ExperimentalWarning|trace-warnings"; }

RAW=$(mktemp)
{
  echo "### BERKSHIRE/GURUS"
  q "Warren Buffett Berkshire Hathaway Greg Abel latest 13F top holdings buys 2026"
  q "Bill Ackman Pershing Square Michael Burry latest 13F stock buys 2026"
  echo "### CONGRESS/POLITICAL"
  q "Nancy Pelosi recent stock trades disclosure 2026 latest buy ticker"
  q "most bought stocks US Congress 2026 Capitol Trades Trump"
  # ── influencer X tweets (Musk active; Trump may be dormant on X) — direct pull ──
  echo "### INFLUENCER TWEETS"
  if [ -f /home/qbao775/.agent-reach/twitter.env ]; then
    source /home/qbao775/.agent-reach/twitter.env
    TW=/home/qbao775/.local/bin/twitter
    for handle in elonmusk realDonaldTrump; do
      echo "--- @$handle ---"
      timeout 90 "$TW" search "from:$handle" -n 8 2>/dev/null \
        | grep -ivE "ExperimentalWarning|trace-warnings" \
        | grep -iE "^\s+text:|createdAt:" | head -16
    done
  fi
} > "$RAW"

"$PY" - "$RAW" "$OUT" <<'PY'
import sys, re, json, datetime, os
raw = open(sys.argv[1]).read(); dst = sys.argv[2]

# company-name → ticker map for names that appear without a $ sign
NAME2TICK = {
    "alphabet": "GOOGL", "google": "GOOGL", "nvidia": "NVDA", "broadcom": "AVGO",
    "microsoft": "MSFT", "apple": "AAPL", "amazon": "AMZN", "meta": "META",
    "palantir": "PLTR", "tesla": "TSLA", "micron": "MU", "tsmc": "TSM",
    "the new york times": "NYT", "occidental": "OXY", "chevron": "CVX",
}
NOISE = {"AI","CEO","US","USD","Q1","Q2","Q3","Q4","13F","NYT","SEC","ETF","CPO","GDP","CPI"}

counts = {}
for m in re.findall(r"\$([A-Za-z]{1,5})\b", raw):
    t = m.upper()
    if t in NOISE or not t.isalpha(): continue
    counts[t] = counts.get(t, 0) + 2  # explicit $TICKER weighted higher
low = raw.lower()
for name, tick in NAME2TICK.items():
    c = low.count(name)
    if c: counts[tick] = counts.get(tick, 0) + c

tickers = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:20]

# overlap with Serenity's current focus (so we can flag confirmation)
serenity = []
try:
    fp = os.path.join(os.path.dirname(dst), "serenity_current_focus.json")
    serenity = json.load(open(fp)).get("top_focus", [])
except Exception:
    pass
overlap = [t for t in tickers if t in serenity]

# influencer tweet snippets (Musk/Trump) — capture the text after the marker
infl = []
seg = raw.split("### INFLUENCER TWEETS", 1)
if len(seg) == 2:
    who = "?"
    for line in seg[1].splitlines():
        m = re.match(r"---\s*@(\w+)", line.strip())
        if m:
            who = m.group(1); continue
        tm = re.search(r"text:\s*['\"]?(.+)", line.strip())
        if tm:
            txt = tm.group(1).strip().strip("'\"")[:180]
            # keep only MARKET-relevant tweets — Musk/Trump post mostly noise
            kw = ("$", "stock", "tesla", "tsla", "ai", "chip", "nvidia", "fed",
                  "rate", "tariff", "econom", "market", "dollar", "trade", "earnings",
                  "spacex", "robot", "energy", "invest", "crypto", "bitcoin")
            if len(txt) > 15 and any(k in txt.lower() for k in kw):
                infl.append({"who": who, "text": txt})
infl = infl[:8]

out = {
    "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    "tickers": tickers,
    "mention_weights": {t: counts[t] for t in tickers},
    "overlap_with_serenity": overlap,
    "influencer_tweets": infl,
    "note": "LAGGED disclosure signal — awareness/cross-check only, secondary to Serenity. Don't chase.",
}
json.dump(out, open(dst, "w"), ensure_ascii=False, indent=1)
print(f"OK tickers={tickers[:10]} | serenity-overlap={overlap}")
PY
rm -f "$RAW"
echo "==== $(date -u +%FT%TZ) smart-money fetch done ===="
