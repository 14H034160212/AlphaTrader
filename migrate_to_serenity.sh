#!/usr/bin/env bash
#
# migrate_to_serenity.sh — finish the physical rename: move the project directory,
# rewire the 22 absolute paths, the systemd service, and the Claude memory anchor.
#
# WHY THIS IS A SEPARATE SCRIPT (not done by the agent in-session):
#   The directory /data/qbao775/AlphaTrader is simultaneously (a) the agent's
#   working directory and (b) the anchor for your Claude memory project
#   (~/.claude/projects/-data-qbao775-AlphaTrader). Moving it from inside a live
#   session would pull the rug out from under the agent and orphan your memory.
#   So you run this yourself, from a normal shell, with no Claude session open on
#   the old path. Review it first — it touches systemd and your memory dir.
#
# AFTER running this: reopen Claude Code in /data/qbao775/SerenityAlphaTrader.
#
set -euo pipefail

OLD=/data/qbao775/AlphaTrader
NEW=/data/qbao775/SerenityAlphaTrader
OLD_MEM="$HOME/.claude/projects/-data-qbao775-AlphaTrader"
NEW_MEM="$HOME/.claude/projects/-data-qbao775-SerenityAlphaTrader"

echo "==> 0. sanity checks"
[ -d "$OLD" ] || { echo "FATAL: $OLD does not exist (already migrated?)"; exit 1; }
[ -e "$NEW" ] && { echo "FATAL: $NEW already exists — refusing to overwrite"; exit 1; }

echo "==> 1. stop the trading service (pauses live trading)"
systemctl --user stop alphatrader.service 2>/dev/null || echo "   (service not running / no user bus — ok)"

echo "==> 2. move the project directory"
mv "$OLD" "$NEW"
cd "$NEW"

echo "==> 3. rewrite the 22 absolute paths inside the repo (github URL left intact)"
grep -rIl "$OLD" --include=*.py --include=*.sh --include=*.md --include=*.json . 2>/dev/null \
  | grep -v '^\./\.git/' \
  | xargs --no-run-if-empty perl -i -pe "s{\Q$OLD\E}{$NEW}g"
echo "   remaining old-path refs (should be 0):"
grep -rIc "$OLD" --include=*.py --include=*.sh --include=*.md . 2>/dev/null | grep -v ':0$' || echo "   0"

echo "==> 4. install the renamed systemd unit"
SVC_DIR="$HOME/.config/systemd/user"
if [ -f "$SVC_DIR/alphatrader.service" ]; then
  sed -e "s#$OLD#$NEW#g" \
      -e "s#Description=AlphaTrader#Description=SerenityAlphaTrader#g" \
      "$SVC_DIR/alphatrader.service" > "$SVC_DIR/serenitytrader.service"
  systemctl --user disable alphatrader.service 2>/dev/null || true
  rm -f "$SVC_DIR/alphatrader.service"
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable serenitytrader.service 2>/dev/null || true
  echo "   -> serenitytrader.service installed (old one removed)"
else
  echo "   (no alphatrader.service found — skipped)"
fi

echo "==> 5. re-anchor Claude memory to the new path"
if [ -d "$OLD_MEM" ] && [ ! -e "$NEW_MEM" ]; then
  mv "$OLD_MEM" "$NEW_MEM"
  echo "   -> memory moved: $NEW_MEM"
else
  echo "   (memory dir missing or target exists — skipped; check manually)"
fi

echo
echo "==> DONE. Next:"
echo "   1) Reopen Claude Code in:  $NEW"
echo "   2) Start trading:          systemctl --user start serenitytrader.service"
echo "      (or: bash $NEW/start.sh)"
echo "   3) Update any external crontab entries that referenced $OLD"
