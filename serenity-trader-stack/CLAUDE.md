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

1. **Execute autonomously — don't wait for confirmation.** (Updated 2026-07-07 — supersedes the original "always confirm first" rule.) User has repeatedly and explicitly granted full autonomous execution authority for the satellite: build AND reduce positions on your own judgment, size them to match the research/conviction already done (per the buckets in rule 5 below) rather than leaving them token-sized, and report after the fact — don't ask first. Still always: verify real available cash/buying power before ordering (no-margin cash account), never chase a same-day spike, stay within the satellite cap, and never fund satellite trades by selling core Plan D holdings. See `~/.claude/.../memory/project_management_mandate.md` (2026-07-07 entry) for the full quote history behind this.

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
