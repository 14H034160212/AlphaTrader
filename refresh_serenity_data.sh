#!/usr/bin/env bash
#
# refresh_serenity_data.sh — pull Serenity's latest data from the yan-labs
# upstream and sync it into BOTH:
#   (1) the AlphaTrader platform copy  .claude/skills/serenity-aleabitoreddit/
#       → serenity_lens.py hot-reloads it via mtime (no engine restart needed),
#         so the trading brain always uses Serenity's latest universe/track-record.
#   (2) the user's published repo 14H034160212/serenity-skills
#       → commit + push ONLY when something actually changed.
#
# yan-labs updates its public repo several times a day (hourly timer + when
# Serenity tweets), so most runs find nothing new and exit quietly.
#
# Cron: */30 * * * *  (every 30 min). Logs to /tmp/serenity_refresh.log.
set -uo pipefail
LOG=/tmp/serenity_refresh.log
exec >>"$LOG" 2>&1
echo "==== $(date -u +%FT%TZ) refresh start ===="

PLAT=/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit
REPO=/home/qbao775/serenity-skills-sync          # persistent working clone of the user's repo
UP_URL=https://github.com/yan-labs/serenity-aleabitoreddit.git

TMP=$(mktemp -d) || { echo "FATAL: mktemp"; exit 1; }
trap 'rm -rf "$TMP"' EXIT
UP="$TMP/upstream"

# 1) shallow-clone yan-labs upstream
if ! git clone --depth 1 -q "$UP_URL" "$UP"; then
    echo "FATAL: clone yan-labs failed (offline?)"; exit 1
fi
echo "upstream: $(head -1 "$UP/data/ticker_stats.txt" 2>/dev/null)"

# helper: sync upstream → a flattened skill dir ($1)
sync_into() {
    local dst="$1"
    mkdir -p "$dst/references" "$dst/analysis" "$dst/data" "$dst/assets"
    rsync -a --delete "$UP/serenity-aleabitoreddit/references/" "$dst/references/"
    rsync -a --delete "$UP/serenity-aleabitoreddit/analysis/"   "$dst/analysis/"
    cp -f "$UP/serenity-aleabitoreddit/SKILL.md" "$dst/SKILL.md"
    # exclude sync_state.json — it's a heartbeat that changes constantly and
    # would cause meaningless pushes every run; we only want real data changes.
    rsync -a --delete --exclude=sync_state.json "$UP/data/" "$dst/data/"
    [ -d "$UP/assets" ] && rsync -a "$UP/assets/" "$dst/assets/"
}

# 2) platform copy — serenity_lens picks it up on next call via mtime
sync_into "$PLAT"
echo "platform copy synced: $PLAT"

# 3) user's published serenity-skills repo — push only on real change
TOKEN=$(git -C /data/qbao775/AlphaTrader config --get remote.origin.url | sed -E 's#https://([^@]+)@.*#\1#')
if [ ! -d "$REPO/.git" ]; then
    git clone -q "https://${TOKEN}@github.com/14H034160212/serenity-skills.git" "$REPO" \
        || { echo "FATAL: clone serenity-skills failed"; exit 1; }
fi
git -C "$REPO" pull -q --no-rebase origin main || true
sync_into "$REPO/serenity-aleabitoreddit"
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    git -C "$REPO" add -A
    git -C "$REPO" -c user.name="Qiming Bao" -c user.email="qiming.bao@xtracta.com" \
        commit -q -m "chore: auto-sync Serenity data from yan-labs upstream ($(date -u +%FT%TZ))"
    if git -C "$REPO" push -q origin main; then
        echo "pushed serenity-skills update"
    else
        echo "WARN: push failed"
    fi
else
    echo "no change in serenity-skills (upstream had nothing new)"
fi
echo "==== $(date -u +%FT%TZ) refresh done ===="
