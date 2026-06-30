#!/usr/bin/env bash
# install-trader-stack.sh (v1.1)
# One-click install: ai-berkshire + serenity-skill + anthropics/financial-services
# + helper scripts (snapshot-portfolio, quote, place-order, refresh-today)
# into ~/.claude/{skills,commands}/ for use with Claude Code Opus 4.8.
#
# Idempotent: re-running is safe; existing files are backed up to .bak.<timestamp>.

set -euo pipefail

STACK_DIR="${STACK_DIR:-$HOME/serenity-trader-stack}"
CLAUDE_DIR="$HOME/.claude"
ALPHATRADER_DIR="${ALPHATRADER_DIR:-/data/qbao775/AlphaTrader}"
INSTALL_MUXUUU_SERENITY="${INSTALL_MUXUUU_SERENITY:-no}"   # default: skip — user already has serenity-chokepoint-analysis
TS="$(date +%Y%m%d-%H%M%S)"

# ─── pretty printing ────────────────────────────────────────────────
B='\033[1m'; G='\033[32m'; Y='\033[33m'; R='\033[31m'; N='\033[0m'
say()  { printf "${B}▸ %s${N}\n" "$*"; }
ok()   { printf "${G}  ✓ %s${N}\n" "$*"; }
warn() { printf "${Y}  ⚠ %s${N}\n" "$*"; }
die()  { printf "${R}  ✗ %s${N}\n" "$*"; exit 1; }

backup_if_exists() {
    local p="$1"
    if [ -e "$p" ] || [ -L "$p" ]; then
        local bak="${p}.bak.${TS}"
        cp -RP "$p" "$bak" 2>/dev/null || mv "$p" "$bak"
        warn "existing $p backed up → $bak"
    fi
}

# ─── 0. Prereqs ─────────────────────────────────────────────────────
say "0. Checking prerequisites"
command -v git    >/dev/null || die "git not found"
command -v claude >/dev/null || warn "claude CLI not in PATH (skills will install anyway)"
command -v python3 >/dev/null || die "python3 not found"
ok "prereqs OK"
mkdir -p "$STACK_DIR" "$CLAUDE_DIR/skills" "$CLAUDE_DIR/commands" "$STACK_DIR/scripts"
ok "dirs ready"

# ─── 1. Clone source repos ──────────────────────────────────────────
clone_or_pull() {
    local url="$1" dir="$2"
    if [ -d "$STACK_DIR/$dir/.git" ]; then
        say "  refreshing $dir"
        git -C "$STACK_DIR/$dir" pull --ff-only --quiet || warn "pull failed, keeping local"
    else
        say "  cloning $dir"
        git clone --depth 1 --quiet "$url" "$STACK_DIR/$dir"
    fi
}
say "1. Cloning 3 source repos into $STACK_DIR"
clone_or_pull https://github.com/xbtlin/ai-berkshire.git           ai-berkshire
clone_or_pull https://github.com/muxuuu/serenity-skill.git         serenity-skill
clone_or_pull https://github.com/anthropics/financial-services.git financial-services
ok "all 3 cloned"

# ─── 2. ai-berkshire: install slash commands ────────────────────────
say "2. Installing ai-berkshire slash commands → ~/.claude/commands/"
INSTALLED=0
for f in "$STACK_DIR/ai-berkshire/skills/"*.md; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    if [ -e "$CLAUDE_DIR/commands/$base" ] && ! grep -q "ai-berkshire" "$CLAUDE_DIR/commands/$base" 2>/dev/null; then
        backup_if_exists "$CLAUDE_DIR/commands/$base"
    fi
    cp -f "$f" "$CLAUDE_DIR/commands/"
    INSTALLED=$((INSTALLED+1))
done
ok "installed $INSTALLED slash commands"

# ─── 3. Disable overlap: bottleneck-hunter (Serenity wins) ──────────
if [ -f "$CLAUDE_DIR/commands/bottleneck-hunter.md" ]; then
    say "3. Disabling ai-berkshire/bottleneck-hunter.md (Serenity skill covers this)"
    mv "$CLAUDE_DIR/commands/bottleneck-hunter.md" "$CLAUDE_DIR/commands/bottleneck-hunter.md.disabled.${TS}"
    ok "disabled"
fi

# ─── 4. serenity-skill (muxuuu) — only if user opts in ──────────────
say "4. serenity-skill (muxuuu/serenity-skill)"
if [ -e "$CLAUDE_DIR/skills/serenity-chokepoint-analysis" ]; then
    warn "you already have ~/.claude/skills/serenity-chokepoint-analysis (overlaps muxuuu)"
    if [ "$INSTALL_MUXUUU_SERENITY" = "yes" ]; then
        warn "INSTALL_MUXUUU_SERENITY=yes → installing anyway (will conflict)"
        SS_DST="$CLAUDE_DIR/skills/serenity-skill"; backup_if_exists "$SS_DST"
        mkdir -p "$SS_DST"
        SS_SRC="$STACK_DIR/serenity-skill"
        [ -f "$SS_SRC/SKILL.md" ] && cp -f "$SS_SRC/SKILL.md" "$SS_DST/"
        for sub in references assets scripts agents; do
            [ -d "$SS_SRC/$sub" ] && cp -R "$SS_SRC/$sub" "$SS_DST/"
        done
        ok "muxuuu serenity-skill installed (alongside existing)"
    else
        ok "skipping muxuuu/serenity-skill — keeping your existing serenity-chokepoint-analysis"
        # but still copy the bottleneck-scorecard.json template into reports/ so it's reusable
        if [ -f "$STACK_DIR/serenity-skill/assets/bottleneck-scorecard.json" ]; then
            cp -f "$STACK_DIR/serenity-skill/assets/bottleneck-scorecard.json" "$STACK_DIR/reports/_bottleneck-scorecard-template.json" 2>/dev/null || mkdir -p "$STACK_DIR/reports" && cp -f "$STACK_DIR/serenity-skill/assets/bottleneck-scorecard.json" "$STACK_DIR/reports/_bottleneck-scorecard-template.json"
            ok "  but borrowed muxuuu's bottleneck-scorecard.json → reports/_bottleneck-scorecard-template.json"
        fi
    fi
else
    SS_DST="$CLAUDE_DIR/skills/serenity-skill"; backup_if_exists "$SS_DST"
    mkdir -p "$SS_DST"
    SS_SRC="$STACK_DIR/serenity-skill"
    [ -f "$SS_SRC/SKILL.md" ] && cp -f "$SS_SRC/SKILL.md" "$SS_DST/"
    for sub in references assets scripts agents; do
        [ -d "$SS_SRC/$sub" ] && cp -R "$SS_SRC/$sub" "$SS_DST/"
    done
    ok "muxuuu serenity-skill installed"
fi

# ─── 5. anthropic equity-research skills ────────────────────────────
say "5. Installing anthropic equity-research skills"
AER_BASE="$STACK_DIR/financial-services/plugins/vertical-plugins/equity-research/skills"
for skill in idea-generation thesis-tracker; do
    if [ -d "$AER_BASE/$skill" ]; then
        DST="$CLAUDE_DIR/skills/$skill"
        backup_if_exists "$DST"
        mkdir -p "$DST"
        cp -R "$AER_BASE/$skill/." "$DST/"
        ok "  installed: $skill"
    else
        warn "  not found: $AER_BASE/$skill"
    fi
done
MR_SRC="$STACK_DIR/financial-services/plugins/agent-plugins/market-researcher"
if [ -d "$MR_SRC" ]; then
    DST="$CLAUDE_DIR/skills/market-researcher"
    backup_if_exists "$DST"; mkdir -p "$DST"
    cp -R "$MR_SRC/." "$DST/"
    ok "  installed: market-researcher"
fi

# ─── 6. Persistent reports/ folder ──────────────────────────────────
say "6. Persistent reports/ folder (git-versioned)"
REPORTS="$STACK_DIR/reports"
mkdir -p "$REPORTS"
if [ ! -d "$REPORTS/.git" ]; then
    git -C "$REPORTS" init --quiet
    cat > "$REPORTS/.gitignore" <<'EOF'
*.log
*.tmp
.DS_Store
snapshots/*.json
EOF
    cat > "$REPORTS/README.md" <<'EOF'
# Trader stack persistent reports

- `<TICKER>/thesis.md`       — running thesis
- `<TICKER>/scorecard.json`  — serenity bottleneck scorecard
- `<TICKER>/updates.md`      — dated update log
- `snapshots/`               — daily portfolio + price snapshots (gitignored to avoid noise)
- `_bottleneck-scorecard-template.json` — reusable scorecard schema

Git-versioned. `git log` to see thesis evolution.
EOF
    git -C "$REPORTS" add -A
    git -C "$REPORTS" commit -m "init" --quiet
    ok "reports/ initialized"
else
    ok "reports/ already a git repo"
fi
mkdir -p "$REPORTS/snapshots"

# ─── 7. Helper scripts ──────────────────────────────────────────────
say "7. Writing helper scripts to $STACK_DIR/scripts/"

# 7a. today.md — current date + market state, regenerated on every run
cat > "$STACK_DIR/scripts/refresh-today.sh" <<EOF
#!/usr/bin/env bash
# Writes serenity-trader-stack/today.md with current dates + market state.
# Run this manually before each Claude Code session, or via cron @hourly.
set -e
OUT="$STACK_DIR/today.md"
{
    echo "# Today (auto-refreshed)"
    echo ""
    echo "- UTC:  \$(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "- NZT:  \$(TZ='Pacific/Auckland' date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "- EDT:  \$(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "- HKT:  \$(TZ='Asia/Hong_Kong' date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""
    echo "## Market state (rough)"
    echo "- US RTH:  9:30-16:00 EDT (M-F)"
    echo "- HK:      9:30-12:00 + 13:00-16:00 HKT (M-F)"
    echo ""
    echo "_Regenerated by scripts/refresh-today.sh — re-read before any new thesis to anchor 'today'._"
} > "\$OUT"
echo "✓ \$OUT updated"
EOF
chmod +x "$STACK_DIR/scripts/refresh-today.sh"
"$STACK_DIR/scripts/refresh-today.sh" >/dev/null
ok "refresh-today.sh + initial today.md"

# 7b. snapshot-portfolio.sh — pull positions from Alpaca + IBKR + Moomoo
cat > "$STACK_DIR/scripts/snapshot-portfolio.sh" <<EOF
#!/usr/bin/env bash
# Pull current holdings from Alpaca + IBKR + Moomoo and dump as markdown.
# Output: ~/serenity-trader-stack/reports/snapshots/portfolio-YYYY-MM-DD.md
#
# Requires AlphaTrader's backend code at $ALPHATRADER_DIR/backend/.
# IBKR + Moomoo need their daemons running; if not, they're skipped gracefully.

set -e
OUT="$STACK_DIR/reports/snapshots/portfolio-\$(date +%Y-%m-%d).md"
mkdir -p "\$(dirname "\$OUT")"

cd "$ALPHATRADER_DIR/backend" 2>/dev/null || { echo "AlphaTrader backend missing"; exit 1; }

conda run -n alphatrader python3 -u - <<'PYEOF' > "\$OUT"
import sys, os, datetime
sys.path.insert(0, '.')
from database import SessionLocal, get_setting

print(f"# Portfolio snapshot — {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC}\n")

# --- Alpaca ---
try:
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
    u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
    api = tradeapi.REST(k,s,u)
    a = api.get_account()
    print("## Alpaca US")
    print(f"- equity: \${float(a.equity):.2f} | cash: \${float(a.cash):.2f}")
    print("- positions:")
    for p in api.list_positions():
        if float(p.market_value) < 1: continue
        print(f"  - {p.symbol}: {float(p.qty):.4f}sh \${float(p.market_value):.2f} pl \${float(p.unrealized_pl):+.2f}")
except Exception as e:
    print(f"## Alpaca\n_(failed: {e})_")

# --- IBKR ---
try:
    from ib_insync import IB
    ib = IB(); ib.connect('127.0.0.1', 4003, clientId=200, timeout=8)
    nl = next((float(r.value) for r in ib.accountSummary() if r.tag == 'NetLiquidation'), 0)
    cash = next((float(r.value) for r in ib.accountSummary() if r.tag == 'TotalCashValue'), 0)
    print(f"\n## IBKR US\n- NetLiq: \${nl:.2f} | cash: \${cash:.2f}")
    pos = ib.positions()
    if pos:
        print("- positions:")
        for p in pos:
            print(f"  - {p.contract.symbol}: {p.position}sh @ \${p.avgCost:.2f}")
    else:
        print("- positions: (none)")
    ib.disconnect()
except Exception as e:
    print(f"\n## IBKR\n_(gateway offline or failed: {type(e).__name__})_")

# --- Moomoo HK ---
try:
    import futu as ft
    ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK,
        host='127.0.0.1', port=11111, security_firm=ft.SecurityFirm.FUTUAU)
    ret, pos = ctx.position_list_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True)
    ret2, info = ctx.accinfo_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True, currency=ft.Currency.HKD)
    print(f"\n## Moomoo HK")
    if ret2 == ft.RET_OK:
        r = info.iloc[0]
        print(f"- cash: HK\${r['cash']:.0f} | power: HK\${r['power']:.0f}")
    if ret == ft.RET_OK and not pos.empty:
        print("- positions:")
        for _, p in pos.iterrows():
            print(f"  - {p['code']}: {p['qty']}sh cost HK\${p['cost_price']:.2f} → HK\${p['nominal_price']:.2f} | pl HK\${p['pl_val']:+.0f} ({p['pl_ratio']:+.1f}%)")
    ctx.close()
except Exception as e:
    print(f"\n## Moomoo HK\n_(OpenD offline or failed: {type(e).__name__})_")
PYEOF
echo "✓ \$OUT"
EOF
chmod +x "$STACK_DIR/scripts/snapshot-portfolio.sh"
ok "snapshot-portfolio.sh"

# 7c. quote.sh — current price for any ticker
cat > "$STACK_DIR/scripts/quote.sh" <<EOF
#!/usr/bin/env bash
# Usage: quote.sh AAPL  |  quote.sh 0700.HK  |  quote.sh 02382.HK
set -e
SYM="\${1:?usage: quote.sh TICKER}"
cd "$ALPHATRADER_DIR/backend" 2>/dev/null || { echo "AlphaTrader missing"; exit 1; }
conda run -n alphatrader python3 -c "
import sys; sys.path.insert(0,'.')
import market_data as md
q = md.get_stock_quote('\$SYM')
if q:
    print(f\"\$SYM: \\\${q.get('current')} ({q.get('change_pct',0):+.2f}% today) | hi/lo \\\${q.get('high','-')}/\\\${q.get('low','-')} | vol {q.get('volume','-'):,}\")
else:
    print(f'\$SYM: no data')
" 2>/dev/null
EOF
chmod +x "$STACK_DIR/scripts/quote.sh"
ok "quote.sh"

# 7d. check-allocation.sh — compute vs target allocation
cat > "$STACK_DIR/scripts/check-allocation.sh" <<EOF
#!/usr/bin/env bash
# Show actual vs target allocation across passive-index / value / chokepoint buckets.
# Targets are read from this file (edit them to change strategy).

TARGET_INDEX_PCT=70
TARGET_VALUE_PCT=20
TARGET_CHOKEPOINT_PCT=8
TARGET_CASH_PCT=2

# These define what counts as "index" vs "value" vs "chokepoint"
INDEX_TICKERS="SPY VOO VTI SPLG QQQ 2800.HK 02800.HK 3033.HK 03033.HK"

cd "$ALPHATRADER_DIR/backend" 2>/dev/null || exit 1
conda run -n alphatrader python3 - <<'PYEOF'
import sys, os
sys.path.insert(0,'.')
from database import SessionLocal, get_setting
import alpaca_trade_api as tradeapi

INDEX = set("SPY VOO VTI SPLG QQQ 2800.HK 02800.HK 3033.HK 03033.HK".split())

db = SessionLocal()
k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
api = tradeapi.REST(k,s,u)

eq = float(api.get_account().equity)
cash = float(api.get_account().cash)
buckets = {'index':0, 'other':0, 'cash':cash}
for p in api.list_positions():
    if float(p.market_value) < 1: continue
    mv = float(p.market_value)
    if p.symbol in INDEX: buckets['index'] += mv
    else: buckets['other'] += mv

print(f"=== Alpaca alocation (equity \${eq:.2f}) ===")
for k_, v in buckets.items():
    pct = v / eq * 100 if eq else 0
    print(f"  {k_:12} \${v:8.2f}  ({pct:5.1f}%)")
print()
print(f"Target: index 70% / value 20% / chokepoint 8% / cash 2%")
idx_pct = buckets['index']/eq*100
delta = idx_pct - 70
print(f"Index actual: {idx_pct:.1f}%  → vs target 70%: {delta:+.1f}pp")
if abs(delta) > 5:
    print(f"⚠️  > 5pp off target — consider rebalancing")
PYEOF
EOF
chmod +x "$STACK_DIR/scripts/check-allocation.sh"
ok "check-allocation.sh"

# 7e. place-order.sh — wrapper for one-shot orders via AlphaTrader broker code
cat > "$STACK_DIR/scripts/place-order.sh" <<EOF
#!/usr/bin/env bash
# Usage: place-order.sh BUY SPY 1.5 limit 760
#        place-order.sh SELL MU 10 market
# Wraps AlphaTrader's existing broker code. Auto-routes US → Alpaca, HK → Moomoo.
set -e
SIDE="\${1:?BUY|SELL}"
SYM="\${2:?ticker}"
QTY="\${3:?quantity}"
TYPE="\${4:-market}"
LIMIT="\${5:-}"

cd "$ALPHATRADER_DIR/backend" 2>/dev/null || exit 1
conda run -n alphatrader python3 -u - <<PYEOF
import sys; sys.path.insert(0,'.')
from database import SessionLocal, get_setting
sym = '\$SYM'; side = '\$SIDE'; qty = float('\$QTY'); otype = '\$TYPE'
limit = '\$LIMIT'

if '.HK' in sym or '.CN' in sym:
    print(f'Routing {sym} → Moomoo (HK/CN)')
    print('Manual via App for now (OpenAPI permission still blocked).')
    print(f'In App: search {sym}, side={side}, qty={qty}, type={otype}, limit={limit}')
else:
    print(f'Routing {sym} → Alpaca (US)')
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
    u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
    api = tradeapi.REST(k,s,u)
    kwargs = dict(symbol=sym, qty=qty, side=side.lower(), type=otype, time_in_force='day')
    if otype == 'limit' and limit:
        kwargs['limit_price'] = float(limit)
        kwargs['extended_hours'] = True
    o = api.submit_order(**kwargs)
    print(f'✓ Submitted: {o.symbol} {o.side} {o.qty}sh {o.type} status={o.status} (id={o.id[:8]})')
PYEOF
EOF
chmod +x "$STACK_DIR/scripts/place-order.sh"
ok "place-order.sh"

# ─── 8. Project CLAUDE.md ───────────────────────────────────────────
say "8. Writing project CLAUDE.md"
PROJECT_CLAUDE="$STACK_DIR/CLAUDE.md"
backup_if_exists "$PROJECT_CLAUDE"
cat > "$PROJECT_CLAUDE" <<EOF
# Serenity-Trader Stack — Project Guide

Personal trader stack for Qiming. Merges three skill sources + helper scripts.

## Always read before any analysis
- \`today.md\` — current date/time anchor (refresh via \`scripts/refresh-today.sh\`)
- \`reports/snapshots/portfolio-YYYY-MM-DD.md\` — latest portfolio snapshot

## Analyses available

### Value picks — ai-berkshire slash commands (Buffett/Munger/段永平/李录)
- \`/investment-team <ticker>\`   — 4-master adversarial team
- \`/dyp-ask <topic>\`             — Duan Yongping persona
- \`/portfolio-review\`            — review current holdings (uses latest snapshot)
- \`/quality-screen <universe>\`
- \`/thesis-tracker\`              — re-evaluate held thesis

### Supply-chain chokepoint — Skills auto-trigger on phrases
- 你已有 \`serenity-chokepoint-analysis\` (中文,Serenity 六步框架) — use this by default
- 你已有 \`serenity-aleabitoreddit\` (track @aleabitoreddit's public calls) — secondary
- Borrowed: \`reports/_bottleneck-scorecard-template.json\` (from muxuuu/serenity-skill assets)

### Screening + thesis tracking — anthropic equity-research skills
- \`idea-generation\` skill (value/growth/quality screens, concrete thresholds)
- \`thesis-tracker\` skill (pillar scorecard + falsifying risks + catalyst calendar)
- \`market-researcher\` skill (6-step deep workflow)

## Helper scripts (call from Bash tool when needed)

| Script | Purpose |
|---|---|
| \`scripts/refresh-today.sh\` | Update \`today.md\` with current dates |
| \`scripts/snapshot-portfolio.sh\` | Pull Alpaca + IBKR + Moomoo positions → markdown snapshot |
| \`scripts/quote.sh <TICKER>\` | Get current price for any ticker (US or HK) |
| \`scripts/check-allocation.sh\` | Compare actual vs target allocation, warn if >5pp off |
| \`scripts/place-order.sh <SIDE> <SYM> <QTY> [TYPE] [LIMIT]\` | One-shot order placement (US auto, HK manual) |

## Operating rules (you, Claude, must follow)

1. **Never autonomously place orders.** Always echo the exact \`place-order.sh\` command for user to confirm; never execute without explicit user request.

2. **Cite sources.** Use \`[UNSOURCED]\` tag when a figure cannot be tied to a named filing or regulator publication.

3. **Researchability rating first.** Before deep-dive, rate target A/B/C per ai-berkshire:
   - A = info-rich (warn: AI output converges with consensus, low alpha)
   - B = mid-tier (mark every imputed number with confidence)
   - C = scarce (switch to first-principles)

4. **Falsifiable thesis.** Every recommendation lists 2-3 specific events that would invalidate it.

5. **Position sizing buckets** (qualitative):
   - 试探 trial: ≤3%  | 标准 standard: ~5%  | 确信 high conviction: 8-10% max
   - Never recommend >15% in any single name except SPY/VOO/2800.HK.

6. **Allocation target:** 70% passive index (SPY/VOO + 2800.HK) / 20% value satellite / 8% chokepoint satellite / 2% cash. Run \`scripts/check-allocation.sh\` to verify before recommending new buys.

7. **Cross-currency routing:**
   - US-listed (no dot suffix or ".") → Alpaca (fractional OK) or IBKR (whole-share only)
   - HK-listed (\`.HK\` suffix) → Moomoo (board-lot only — \`scripts/quote.sh\` returns lot_size)
   - Never assume — confirm via snapshot before recommending qty.

8. **Persistent state.** Save every new thesis to \`reports/<TICKER>/thesis.md\`; auto-\`git commit\` after each cycle.

## Boot sequence (before any Claude Code session)

\`\`\`
cd $STACK_DIR
./scripts/refresh-today.sh      # anchors today's date
./scripts/snapshot-portfolio.sh # pulls fresh positions (requires brokers reachable)
\`\`\`

Then start Claude Code and run \`/portfolio-review\` first.
EOF
ok "project CLAUDE.md"

# ─── 9. Update ~/.claude/skills/ to symlink to stack scripts ─────────
# Not done — skills don't have a 'helper scripts' concept; CLAUDE.md is enough.

# ─── 10. Optional: pip-install ai-berkshire python tools dependencies ─
say "9. Installing ai-berkshire Python tool dependencies (optional)"
if [ -f "$STACK_DIR/ai-berkshire/requirements.txt" ]; then
    python3 -m pip install --quiet --user -r "$STACK_DIR/ai-berkshire/requirements.txt" 2>/dev/null \
        && ok "deps installed" || warn "pip install failed (tools still copied; install manually if needed)"
else
    ok "(no requirements.txt; ai-berkshire uses stdlib only)"
fi

# ─── 11. Inventory + smoke test ─────────────────────────────────────
say "10. Final inventory"
echo
echo "Slash commands (top 8):"
ls -1 "$CLAUDE_DIR/commands/"*.md 2>/dev/null | sed 's|.*/|    |' | head -8
echo
echo "Skills installed:"
ls -1d "$CLAUDE_DIR/skills/"*/ 2>/dev/null | sed 's|.*/||;s|/$||;s|^|    |'
echo
echo "Helper scripts:"
ls -1 "$STACK_DIR/scripts/" | sed 's|^|    |'
echo
echo "Stack location: $STACK_DIR"
echo "Reports (git):  $STACK_DIR/reports/"
echo "today.md:       $STACK_DIR/today.md"
echo

printf "${G}═══ Install complete ═══${N}\n\n"
echo "Quick test:"
echo "  ${B}$STACK_DIR/scripts/refresh-today.sh${N}        # OK?"
echo "  ${B}$STACK_DIR/scripts/quote.sh SPY${N}            # needs AlphaTrader env"
echo "  ${B}$STACK_DIR/scripts/snapshot-portfolio.sh${N}   # pulls 3 brokers"
echo "  ${B}$STACK_DIR/scripts/check-allocation.sh${N}     # vs target 70/20/8/2"
echo
echo "Then in Claude Code:  ${B}/investment-team MU${N}"
echo "Or:                   ${B}用 serenity 卡点分析 AI 算力扩张主题${N}"
echo
echo "Uninstall:"
echo "  Manual — see ~/.claude/{skills,commands}/ + remove via 'rm -rf'"
echo "  Backups of overwritten files: *.bak.${TS}"
