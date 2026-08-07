# FTK (Flotek Industries) — Thesis

_Backfilled 2026-08-07 after the position sat undocumented since entry (2026-08-04),
which repeatedly caused crossvalidate_satellite.py to misread "no saved thesis" as
"thesis broken" — see [[project_crossvalidate_missing_thesis_false_positive]]._

## What the catalyst actually is (corrected 2026-08-07)

Flotek is an oilfield-chemicals company with a power-generation/electrification
segment (PWRtek). On 2026-08-03 it announced a role in a Puerto Rico Electric
Power Authority (PREPA) 10-year power project — **but the internal record this
position was entered on overstated it**:

- Flotek is a **supplier to the lead contractor** (Power Expectations LLC),
  not the counterparty on an exclusive 10-year/400MW contract of its own.
- Scope: up to 6 pairs of PWRtek smart voltage-regulation/distribution skids
  + up to 40MW of self-generation equipment.
- Revenue: ~$40M/year at full run-rate, ~$400M ten-year backlog — real, but a
  fraction of "400MW power plant contract" read at face value.
- **Timing: equipment deliveries start Q4 2026, main equipment Q1 2027** —
  this is a multi-quarter-out revenue catalyst, not something that shows up in
  near-term earnings. The stock's move since 8/3 is a re-rating on the
  headline/backlog, well ahead of any cash-flow realization.

Sources: [Flotek IR](https://ir.flotekind.com/2026-08-03-Flotek-Awarded-10-Year-Contract-to-Support-a-400-MW-Power-Project-for-Puerto-Rico-Electric-Power-Authority) · [PRNewswire](https://www.prnewswire.com/news-releases/flotek-awarded-10-year-contract-to-support-a-400-mw-power-project-for-puerto-rico-electric-power-authority-302840776.html) · [StockTitan](https://www.stocktitan.net/news/FTK/flotek-awarded-10-year-contract-to-support-a-400-mw-power-project-3btyhea94gan.html)

## Position history (why the cost basis looks the way it does)

Entered 2026-08-04 as one of 12 satellite catalyst picks. On 2026-08-06 between
15:00–16:00 UTC, four consecutive ROTATE_TO judge calls (13:31/13:51/14:33/15:15)
consolidated the whole satellite book down to FTK+RKLB — each individual call
added to FTK **after** it had already run (e.g. buying $36.07→$37.08 on top of
an already +11% move that session). This was chase-buying, the exact opposite
of [[feedback_buy_dips_sell_strength]]. The pre-cascade +31% gain from the
original 8/4 entry no longer reflects the current lot — the present cost basis
is materially higher. Structural fix already shipped same-day: 35% single-name
concentration cap + 2/day rotation cap in `daily_open_daytrade.py`.

## Falsifiable checks

1. PREPA/Power Expectations LLC deliveries slip past Q1 2027 or the contract
   is amended/cancelled → thesis weakens materially.
2. No confirmed order flow or follow-on contract news by Q4 2026 (when
   deliveries are supposed to start) → re-assess before any further add.
3. Position size relative to total account — currently the single biggest
   risk is **concentration** (FTK+RKLB = ~92% of the account as of 8/6, still
   ~62%/29% as of 8/7), independent of whether this specific thesis holds.

## Current read (2026-08-07)

Thesis-level: **HOLD** — contract is real, no negative news, +3.8% flat over
3 days isn't deterioration. Position-level: **should be trimmed toward the
35% cap** — not because the thesis broke, but because concentration risk this
large conflicts with the survival-first mandate regardless of how good any
single thesis is. Awaiting explicit go-ahead before executing the trim size
given the premise (which gain is actually being "locked in") changed from
what was reported the previous 8 times.
