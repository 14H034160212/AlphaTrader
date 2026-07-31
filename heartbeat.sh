#!/bin/bash
# Heartbeat for the dead-man's-switch failsafe (.github/workflows/deadman.yml).
# Force-pushes a fresh empty commit to the `heartbeat` branch every run.
# Cron: */5 * * * *  -- if these stop arriving for 30+ min during market
# hours, the GitHub Actions failsafe liquidates all stock positions to SGOV.
# 4b825dc... is git's well-known empty-tree object; each beat is a single
# parentless commit, force-replaced every time (no history growth).
cd /data/qbao775/AlphaTrader || exit 1
commit=$(git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m "heartbeat $(date -u +%FT%TZ)") || exit 1
git push -q origin "+${commit}:refs/heads/heartbeat"
