# Serenity-Trader Stack — Project Guide

Personal trader stack for Qiming. Merges three skill sources + helper scripts.

## Always read before any analysis
- `today.md` — current date/time anchor (refresh via `scripts/refresh-today.sh`)
- `reports/snapshots/portfolio-YYYY-MM-DD.md` — latest portfolio snapshot

## Analyses available

### Value picks — ai-berkshire slash commands (Buffett/Munger/段永平/李录)
- `/investment-team <ticker>`   — 4-master adversarial team
- `/dyp-ask <topic>`             — Duan Yongping persona
- `/portfolio-review`            — review current holdings (uses latest snapshot)
- `/quality-screen <universe>`
- `/thesis-tracker`              — re-evaluate held thesis

### Supply-chain chokepoint — Skills auto-trigger on phrases
- 你已有 `serenity-chokepoint-analysis` (中文,Serenity 六步框架) — use this by default
- 你已有 `serenity-aleabitoreddit` (track @aleabitoreddit's public calls) — secondary
- Borrowed: `reports/_bottleneck-scorecard-template.json` (from muxuuu/serenity-skill assets)

### Screening + thesis tracking — anthropic equity-research skills
- `idea-generation` skill (value/growth/quality screens, concrete thresholds)
- `thesis-tracker` skill (pillar scorecard + falsifying risks + catalyst calendar)
- `market-researcher` skill (6-step deep workflow)

## Helper scripts (call from Bash tool when needed)

| Script | Purpose |
|---|---|
| `scripts/refresh-today.sh` | Update `today.md` with current dates |
| `scripts/snapshot-portfolio.sh` | Pull Alpaca + IBKR + Moomoo positions → markdown snapshot |
| `scripts/quote.sh <TICKER>` | Get current price for any ticker (US or HK) |
| `scripts/check-allocation.sh` | Compare actual vs target allocation, warn if >5pp off |
| `scripts/place-order.sh <SIDE> <SYM> <QTY> [TYPE] [LIMIT]` | One-shot order placement (US auto, HK manual) |

## Operating rules (you, Claude, must follow)

1. **Execute autonomously on REDUCING/monitoring — but BUYS need explicit confirmation.** (Updated 2026-07-07 for reduce/monitor; **narrowed 2026-07-13 for buys — read carefully, this reverses part of the 2026-07-07 grant.**) User originally granted full autonomous execution authority for the satellite (build AND reduce positions on your own judgment, report after the fact). On 2026-07-13 the user said **"我什么时候让你买你再买"** (only buy when I explicitly tell you to) and confirmed — when asked to scope it — that this applies to **every new buy**: the satellite candidate screen, core-portfolio re-entry, and the SKHY/MU/META long-term entries. All the analysis/watching/gating logic still runs autonomously and still determines when a target is "ready" (chase-guards, the 4-gate core re-entry regime check, the BULLISH+paid-escalation candidate screen, the watch-and-wait entry logic) — reaching "ready" now emails/logs an alert instead of firing an order. An order only executes once Claude creates the matching `.ENTRY_CONFIRMED_<NAME>` marker file in `~/serenity-trader-stack/`, and only in direct response to the user explicitly naming that buy in conversation — never inferred, never pre-created. **Reducing/selling/trimming a position is UNCHANGED — still autonomous, still report-after-the-fact** (e.g. SKHY's $200 take-profit sell, or `crossvalidate_satellite.py`'s advisory TRIM/EXIT calls). Still always: verify real available cash/buying power before ordering, never chase a same-day spike, stay within the satellite cap, never fund satellite trades by selling core Plan D holdings. See `~/.claude/.../memory/project_management_mandate.md` and `PLAN_D.md`'s 2026-07-13 entries for the full quote history behind both the original grant and this narrowing.

2. **Cite sources.** Use `[UNSOURCED]` tag when a figure cannot be tied to a named filing or regulator publication.

3. **Researchability rating first.** Before deep-dive, rate target A/B/C per ai-berkshire:
   - A = info-rich (warn: AI output converges with consensus, low alpha)
   - B = mid-tier (mark every imputed number with confidence)
   - C = scarce (switch to first-principles)

4. **Falsifiable thesis.** Every recommendation lists 2-3 specific events that would invalidate it.

5. **Position sizing buckets** (qualitative):
   - 试探 trial: ≤3%  | 标准 standard: ~5%  | 确信 high conviction: 8-10% max
   - Never recommend >15% in any single name except SPY/VOO/2800.HK.

6. **Allocation target:** 70% passive index (SPY/VOO + 2800.HK) / 20% value satellite / 8% chokepoint satellite / 2% cash. Run `scripts/check-allocation.sh` to verify before recommending new buys.

7. **Cross-currency routing:**
   - US-listed (no dot suffix or ".") → Alpaca (fractional OK) or IBKR (whole-share only)
   - HK-listed (`.HK` suffix) → Moomoo (board-lot only — `scripts/quote.sh` returns lot_size)
   - Never assume — confirm via snapshot before recommending qty.

8. **Persistent state.** Save every new thesis to `reports/<TICKER>/thesis.md`; auto-`git commit` after each cycle.

## Boot sequence (before any Claude Code session)

```
cd /home/qbao775/serenity-trader-stack
./scripts/refresh-today.sh      # anchors today's date
./scripts/snapshot-portfolio.sh # pulls fresh positions (requires brokers reachable)
```

Then start Claude Code and run `/portfolio-review` first.
