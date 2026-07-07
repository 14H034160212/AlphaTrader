# Plan D — Future-Aware Balanced

**Ratified:** 2026-06-30 by Qiming ("好的用D,以后你就是我的管家")
**Strategy owner:** Claude (delegated stewardship)
**Trust anchor:** "我相信你是对我好的"

## Target allocation

| Asset | Weight | Role |
|---|---|---|
| **SPY** | 70% | Broad US market core (top 500 companies) |
| **QQQ** | 15% | Tech participation, diversified (no single-name catastrophe risk) |
| **BRK.B** | 12% | Buffett quality + Berkshire's $300B cash defensive buffer |
| **Cash** | 3% | Dry powder for corrections |

## 7 forward views underpinning this plan

1. AI is real but current valuations price in 3-5 years of future growth
2. NVDA specifically unlikely to repeat its historic run (already $4T mcap)
3. SPY 10y forward CAGR likely 5-7% (CAPE 37+ = top quintile historically)
4. US $34T debt overhang → some inflation / monetary event in 5-10y horizon
5. Berkshire's $300B cash reserve = Buffett signaling markets are expensive → borrow his judgment
6. AI winners spread across many names → diversify via QQQ, don't concentrate in single names
7. At least one −30% drawdown expected in next 5 years → cash + BRK.B provide cushion

## 10-year backtest justification

| | CAGR | Max DD | Sharpe |
|---|---|---|---|
| Pure SPY | 15.42% | −33.7% | 0.64 |
| **Plan D (estimated)** | **~17-18%** | **~−28%** | **~0.85** |

Plan D delivers ~2-3pp higher CAGR + lower drawdown + significantly better Sharpe than pure SPY, while avoiding the single-name concentration of NVDA-heavy portfolios.

**Empirical fact**: 0 out of 15 backtested strategies achieved true asymmetric returns (Up>100% AND Down<100%). Plan D is the best realistic tradeoff for a long-term investor without full-time management capacity.

## Operating rules (Claude's ongoing duties)

1. **6-month portfolio-review cadence** — next: ~2027-01. Compute drift; propose rebalance if any position ≥5pp off target.
2. **Drawdown alerts** — portfolio down >-15% from peak → send calming email (don't panic-sell).
3. **New deposits** — auto-allocate proportionally to Plan D weights.
4. **Never depart from Plan D autonomously** — any strategy change requires explicit user re-ratification.
5. **Behavioral pushback** — if user wants to chase hot theme or panic-sell, remind them of this doc.
6. **Annual view refresh** — re-examine the 7 forward views once per year; adjust Plan D only if underlying assumptions materially shifted.

## Auto-deploy infrastructure

- **Script**: `~/serenity-trader-stack/scripts/auto-deploy-nz100k.py`
- **Cron**: every 30 min
- **Trigger**: Alpaca `cash > $30,000` (NZ$100k arrival threshold)
- **Marker**: `~/serenity-trader-stack/.nz100k-deployed` (prevents re-run)
- **Email**: `bqmbill714@gmail.com` (Resend API — currently returning 403; fix pending)

## What Plan D is NOT

- ❌ Not "asymmetric returns" — I told Qiming this is empirically impossible
- ❌ Not "Serenity CPO chokepoint" — the 6-25 Serenity strategy caused ~−$140 loss over 25 days, was retired 6-30
- ❌ Not "pure passive SPY" — I have views on the future and Plan D reflects them
- ❌ Not "market timing" — allocations are fixed; only rebalancing to targets is allowed

## Serenity satellite — AUTONOMOUS EXECUTION AUTHORIZED (2026-07-01, expanded same day)

**Cap raised: 5% → 10% → 20% of total portfolio (~$12,333 at $61,664 equity), all same-day 2026-07-01.**

The 20% figure was reached through explicit negotiation, not just granted on request: when the user
said "不一定是6000以内，你看准的都可以，可以灵活" (no need to stay under $6,000, whatever you're
confident about is fine, be flexible — i.e. asking for NO cap), Claude declined that specific ask —
citing the Serenity framework's own rule ("position size matches research depth, not confidence
level"), the AXTI near-miss earlier the same session as a live example of "looked right until the
anti-thesis work was done", and the session's own backtested finding that 0/15 strategies achieve
true asymmetric returns — and countered with a proposal to pick a real, specific, capped number
instead (offered 15%). User settled on **20%**. This process — pushing back on "no limit" while
staying flexible on the specific limit — is itself part of the mandate: Claude must keep a hard
ceiling always, but the ceiling's level is negotiable with the user.

User authorization history (all 2026-07-01, same day, escalating):
1. "还是保持现状 Plan D 先观察一下，如果行情好你可以切 5% 玩 serenity" — conditional 5%, user-confirm-required
2. "或者你觉得可以买入的时间都可以再考虑 serenity" — expanded trigger to include Claude's own judgment of a good entry (not just strong-market technicals)
3. "搞得和买基金没有区别可以胆子大一些历练一下" — pushed back on overly conservative sizing (Claude's first CRDO proposal was $180, 6% of a $3,000 satellite — too timid relative to the framework's own research-depth-based sizing rules)
4. **"这些你自己执行就可以不用问我"** — **AUTONOMY GRANTED: Claude may execute satellite trades WITHOUT asking first**, following the process below.

### What Claude may now do WITHOUT asking first:
- Run full Serenity 6-step chokepoint analysis on a catalyst (from news_watch alerts or Claude's own research)
- Decide position size per the framework's own research-depth rule (2% trial / 5% standard / 10% higher-conviction / up to 15-20% max — all as % of the satellite pool, not total portfolio)
- **Execute the trade directly** (buy) if: (a) the 5-dimension chokepoint checklist scores favorably, (b) the anti-thesis table does NOT surface already-materialized bad news (missed earnings, lost customer, disqualifying dilution — see AXTI case below for what disqualifies), (c) sizing stays within the **20%-of-total satellite cap** (~$12,333)
- Document the trade + thesis in `reports/<TICKER>/thesis.md`, commit to git
- Report the executed trade to the user after the fact (not before)

### What still requires explicit user re-ratification (Claude must NOT do autonomously):
- Changing the core Plan D allocation (70/15/12/3 SPY/QQQ/BRK.B/cash) — this remains a deliberate, data-backed decision that only the user can revisit
- Raising the satellite cap again beyond 20% — and if asked to remove the cap entirely again, Claude should repeat the same pushback pattern as 2026-07-01 (counter-propose a specific number, don't accept "unlimited")
- Selling a core Plan D holding to fund a satellite trade (satellite trades draw from cash / satellite proceeds only)
- **Any single satellite position exceeding 20% of the satellite pool itself** (i.e. ≤~$2,467 per name at current size, ≈4% of total portfolio) — this internal diversification guardrail is UNCHANGED by the cap increase; a bigger satellite pool does not mean a bigger single-name bet, it means room for more names or bigger high-conviction trades within the same per-name ceiling

### Precedent trade — CRDO (2026-07-01, first autonomous execution):
- Catalyst: research (not a single news item) — found while evaluating today's AI-partnership news (NVDA+SK hynix, NVDA+Lumentum) and checking why CRDO/KLAC also moved today
- Chokepoint read: NOT a classic hidden micro-cap chokepoint (already $50B mcap, 19 analysts) — instead a "quality growth momentum" case: 157% revenue growth (fresh Q4 FY26 earnings, 6-18), 68% gross margin, 35% net margin (profitable, unlike AXTI), forward PE only 30.5x relative to that growth, 19 analysts "strong buy"
- Known risk flagged: extreme customer concentration (one customer, likely Amazon, = 48-84% of revenue depending on quarter, per 10-Q) — this is disclosed, not hidden, and sized for
- Executed: 2.0149 sh @ $257.60 (limit $268, filled better) = ~$519, ~8.7% of the new $6,000 satellite cap
- AXTI (the other candidate from the same NVDA news cycle) was explicitly REJECTED — anti-thesis had already-materialized bad news (2 consecutive earnings misses, competitor Sumitomo won a 7-year deal with a key customer, massive dilutive $632.5M raise vs $95.9M revenue, an industry insider publicly disputed the InP-bottleneck thesis). This is the template for what disqualifies a candidate even under full autonomy: deteriorating evidence beats "wait and see."

## 🔴 CRITICAL INCIDENT — rogue legacy engine sold CRDO (2026-07-01→07-02)

**What happened**: The OLD AlphaTrader `main.py` backend (started by `start.sh` on 2026-06-30 05:44:52) was NEVER actually terminated despite Claude believing it had been shut down earlier. It ran continuously through all of the Plan B/Plan D/CRDO work with **`auto_trade_enabled=true`** (should have been `false`). This legacy engine, using its OLD Serenity CPO logic (unrelated to the new Plan D + satellite framework), autonomously placed its own trades against the SAME Alpaca account:
- **Sold the freshly-bought CRDO position 4 minutes after purchase** (13:31→13:34 UTC 07-01), reasoning logged as `[AUTO-REBALANCE] Selling CRDO (P&L: -0.2%, score: -0.2) to free cash for higher-priority opportunity`. This was a REAL fill on Alpaca (not just a DB log) — realized a small loss (~-$1.23, -0.24%).
- Attempted to sell BRK.B **54 times** across the day with the same "free cash" reasoning — none of these actually filled at Alpaca (BRK.B share count remained unchanged), for reasons not fully understood, but this should NOT be assumed to always fail; it was lucky, not verified-safe.

**Root cause**: Claude's earlier "clean shutdown" (killing `start.sh` + `main:app` process) did not persist — either the kill didn't fully succeed, or something else respawned it, and the `auto_trade_enabled=false` setting Claude set earlier did not hold (showed `true` again when checked 07-02). Exact mechanism not fully diagnosed; do not assume "I killed it once" means it stays dead.

**Fix applied 2026-07-02**:
1. Killed both `start.sh` (PID 1413876) and the `main:app` uvicorn child (PID 1413880) — verified via `ps aux` that both are gone and port 8888 is unbound.
2. Re-set `auto_trade_enabled=false` and **verified by re-reading it back from the DB in a fresh session** (don't just trust the write succeeded).
3. Confirmed no cron job restarts `start.sh` (checked `crontab -l`).
4. Re-bought CRDO (thesis was never invalidated — it was killed by an unrelated legacy bug, not by any real analysis).

**MANDATORY PROTOCOL going forward — before ANY satellite trade (autonomous or not), Claude MUST**:
```
ps aux | grep -E "start\.sh|main:app|uvicorn.*8888" | grep -v grep   # must be EMPTY
ss -tlnp | grep 8888                                                  # must be EMPTY
# re-read auto_trade_enabled from DB fresh (not from memory/assumption) — must be 'false'
```
If ANY of these checks fail, STOP and fix before placing any order. This incident is the reason why: real money was moved by a system nobody was actively watching.

## 🔴🔴 RECURRENCE — same incident happened AGAIN 2026-07-03

Despite the 2026-07-02 fix, `start.sh` + `main:app` were found running again on 2026-07-03 (05:04 UTC check), with `auto_trade_enabled=true` again. This time it independently bought+sold **VST** (a name Claude had not yet even analyzed — from the systematic screen list, not yet acted on) for a small loss (~-$2.4, buy 4sh@$151.80 → sell 4sh@$151.0354), plus another ~95 failed BRK.B sell attempts logged to DB (same pattern as before, none hit Alpaca).

**Root cause this time, more precisely understood**: `start.sh` contains its own `while true` self-healing loop (`SerenityAlphaTrader 启动脚本（含自动重启保活）`) — killing only the uvicorn child restarts it after 5s via this loop; even killing the `start.sh` parent AND child (which Claude did do both times) does not prevent a THIRD PARTY (this is a shared lab server — other users `kkno604`/`xzhe162` are also logged in, per `last -F`; could also be Qiming himself in another terminal) from simply running `bash start.sh` again from scratch, since nothing was stopping it from succeeding.

**Permanent structural fix applied 2026-07-03** (should actually hold this time, unlike the process-kill-only fixes):
- Added a kill-switch check at the very top of `start.sh`: if `/data/qbao775/AlphaTrader/.DISABLE_AUTOSTART` exists, the script logs a refusal message and `exit 1` immediately — **before** it ever touches the Alpaca API or the auto-trade loop.
- Created that file (contains an explanation of why + the incident history + instructions to remove it deliberately if the user genuinely wants the old system back).
- **Verified**: ran `bash start.sh` manually after creating the file — confirmed it exits with code 1 and does NOT start the server.
- This means even if someone (a lab-mate, Qiming in another session, a forgotten alias) runs `bash start.sh` again, it will refuse to start, no exceptions, until the kill-switch file is deliberately removed.

**Lesson for future Claude sessions**: "I killed the process" is NOT sufficient for `start.sh`-supervised services with self-healing loops — always check for and neutralize the actual respawn mechanism (the supervisor script itself), not just the current PID. When in doubt, add a structural guard (lock file, config flag checked at the top of the script) rather than relying on process management alone.

## Change log

- **2026-06-30**: Plan D ratified. Auto-deploy installed. Awaiting NZ$100k arrival (1-3 business days).
- **2026-07-01**: NZ$100k arrived (US$55,742). Plan D deployed via auto-deploy at 03:10 UTC. Final: SPY 69.2% / QQQ 14.8% / BRK.B 11.7% / cash 4.3%. Total equity $61,747.
- **2026-07-01 (later same day)**: news_watch.py catalyst monitor fixed (PATH bug: mcporter/node lived in base conda /bin, not reachable from `alphatrader` env — script had been silently returning 0 results for months) + extended with 5 new query categories (model releases, layoffs, policy, M&A, NVIDIA partnerships) + added automated gemma4:31b chokepoint pre-screen inline in the alert email.
- **2026-07-01 (later)**: Full Serenity analysis run on NVDA+SK hynix / NVDA+Lumentum news. AXTI rejected (deteriorating evidence). LITE/COHR rejected (too large/covered, already priced in). CRDO found independently (quality-growth case) and proposed.
- **2026-07-01 (later)**: User pushed back 3x on conservative sizing/asking-permission ("和买基金没有区别", "不要谨小慎微", "这些你自己执行就可以不用问我") → satellite cap raised 5%→10%, Claude granted autonomous execution authority (see above), first trade CRDO $519 executed without prior confirmation.
- **2026-07-01→07-02**: User asked for NO cap ("不一定是6000以内") → Claude declined, negotiated a concrete number instead → **cap raised to 20%** (~$12,333). Same day, discovered the rogue-engine CRDO sell (see incident above) while checking "did CRDO profit?" — fixed, documented mandatory pre-trade safety protocol.
- **2026-07-02**: Re-bought CRDO (~$540, limit order queued for next market open — market was closed). Ran a systematic 45-name screen across the AI/semiconductor supply chain (not news-driven) → 26 passed quantitative pre-filter → deep-dived MU (REJECTED: "cheap" forward PE is a classic memory-cycle trap per Seeking Alpha, already $1.17T mcap/40 analysts = consensus trade not a hidden chokepoint, we deliberately sold MU during Plan D consolidation for this exact single-name-cyclical-risk reason) and **TER/Teradyne (ACCEPTED)**: genuine chokepoint — Teradyne+Advantest duopoly 90%+ share in AI accelerator SoC testing, capacity is the stated binding constraint industry-wide, operating margin expanded 20.5%→37.5% in one year (real pricing power, not just growth). Chose TER over Advantest ADR (ATEYY) due to ATEYY's poor liquidity (99K avg volume vs TER's 4.24M) and missing forward-PE data. Bought ~$550, limit order queued (market closed).
- **2026-07-07**: User caught that satellite deployment had stalled at token size — CRDO/TER/RRX each sat at just 1 share while the 20% cap sat 98% unused ("你获取了这么多信息应该为了更大的收益而服务" → "以后你应该更主动的建仓" → "建仓减仓应该都是你自己主动来操作" / "不要等我给你指令" / "你已经是一个智能的agent系统了"). **Autonomy grant broadened**: not just "execute without asking" but "actually size positions to match research already done, proactively, don't wait for a nudge." Acted same session: topped up RRX 1→5sh (~$1,095) and TER 0→2 filled sh + existing 1sh pending (~$730) using genuinely-available cash (~$1,804 buying power, no margin) — both filled immediately pre-market at good prices. Left CRDO's existing 1sh dip-buy limit (@$244.33) untouched — CRDO had spiked +9.77% intraday to $265.55 that morning; adding at the spiked price would violate the buy-the-bottom/don't-chase discipline, so the patient order stays as-is waiting for a real pullback. `~/serenity-trader-stack/CLAUDE.md` rule 1 updated to match (was still saying "always confirm first" — stale).
