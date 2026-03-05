"""
Kelly Criterion Position Sizer — 凯利公式最优仓位

The Kelly Criterion finds the fraction of capital to bet that maximises
long-run geometric growth rate.

  f* = (p·b - q) / b

  p  = probability of winning  (AI confidence score)
  q  = 1 - p
  b  = reward/risk ratio  = (target_price - entry) / (entry - stop_loss)

We apply fractional Kelly (default: half-Kelly) because:
  1. Confidence scores are imperfect estimators of true win probability.
  2. Half-Kelly cuts drawdowns roughly in half with only modest return reduction.
  3. Full Kelly is very aggressive and leads to large swings.

Additional caps:
  - max_single_position_pct: no single trade > X% of portfolio (default 20%)
  - min_rr_ratio: skip trades with reward/risk < 1.5 (default)
"""

import logging

logger = logging.getLogger(__name__)

# ── Configuration defaults ────────────────────────────────────────────────────
KELLY_FRACTION   = 0.5    # half-Kelly (industry standard for imperfect signals)
MAX_POSITION_PCT = 0.20   # max 20% of portfolio in any single name
MIN_RR_RATIO     = 1.5    # minimum reward:risk to consider a trade


def kelly_fraction(confidence: float, reward: float, risk: float) -> float:
    """
    Pure Kelly fraction given win probability and reward/risk.

    Args:
        confidence: probability of profitable outcome (0–1)
        reward:     expected gain if correct (e.g. target_price - entry)
        risk:       expected loss if wrong (e.g. entry - stop_loss)

    Returns:
        Kelly fraction f* (may be negative → no trade)
    """
    if risk <= 0 or reward <= 0:
        return 0.0
    b = reward / risk          # reward-to-risk ratio
    p = max(0.0, min(1.0, confidence))
    q = 1.0 - p
    return (p * b - q) / b


def kelly_position_size(
    confidence: float,
    current_price: float,
    target_price: float,
    stop_loss: float,
    portfolio_value: float,
    kelly_multiplier: float = KELLY_FRACTION,
    max_position_pct: float = MAX_POSITION_PCT,
    indicators: dict = None,
) -> dict:
    """
    Calculate optimal position size using fractional Kelly criterion.
    Incorporates a 'Mean Reversion' penalty if stock is overextended.
    """
    if current_price <= 0 or portfolio_value <= 0:
        return _no_trade("Invalid prices or zero portfolio value")

    # Mean Reversion Modifier
    mr_mod = 1.0
    mr_reason = ""
    if indicators:
        dist_ma200 = indicators.get("dist_from_ma200_pct", 0)
        # If >20% above MA200, start penalizing size
        if dist_ma200 > 20:
            mr_mod = max(0.2, 1.0 - (dist_ma200 - 20) / 40.0)
            mr_reason = f" (Mean Reversion Penalty: {mr_mod:.1f}x due to {dist_ma200:.1f}% MA200 dist)"

    if stop_loss <= 0 or stop_loss >= current_price:
        # No stop loss provided — use fixed 5% risk as a proxy
        stop_loss = current_price * 0.95
        logger.debug("[Kelly] No valid stop_loss — using 5% proxy")

    if target_price <= current_price:
        return _no_trade("target_price must be above current_price for BUY")

    reward = target_price - current_price
    risk   = current_price - stop_loss
    rr     = reward / risk

    if rr < MIN_RR_RATIO:
        return _no_trade(
            f"Reward/risk {rr:.2f} < minimum {MIN_RR_RATIO} — trade skipped"
        )

    f_raw = kelly_fraction(confidence, reward, risk)

    if f_raw <= 0:
        return _no_trade(
            f"Kelly fraction is negative ({f_raw:.3f}) — expected value negative"
        )

    # Apply fractional Kelly, Mean Reversion modifier, and hard cap
    f_adj   = f_raw * kelly_multiplier * mr_mod
    f_final = min(f_adj, max_position_pct)

    dollar_amount = portfolio_value * f_final

    reason = (
        f"Kelly({confidence:.0%} conf, {rr:.1f}x R:R) → "
        f"raw={f_raw:.1%}, adjusted={f_adj:.1%}{mr_reason}, "
        f"capped={f_final:.1%} → ${dollar_amount:,.2f}"
    )
    logger.info(f"[Kelly] {reason}")

    return {
        "dollar_amount":  round(dollar_amount, 2),
        "position_pct":   round(f_final * 100, 2),
        "kelly_raw":      round(f_raw, 4),
        "kelly_adj":      round(f_adj, 4),
        "rr_ratio":       round(rr, 2),
        "reason":         reason,
        "skip":           False,
    }


def kelly_position_size_sell(
    confidence: float,
    current_price: float,
    stop_loss: float,      # used as "how high could it go" = upside risk
    target_price: float,   # our downside target (lower than current for SELL)
    portfolio_value: float,
    current_position_value: float,
    kelly_multiplier: float = KELLY_FRACTION,
    max_position_pct: float = MAX_POSITION_PCT,
) -> dict:
    """
    Kelly sizing for SELL (reduce existing long position).
    Calculates what fraction of the current position to sell.

    Returns same dict structure as kelly_position_size but:
        dollar_amount = dollars worth of stock to sell
        position_pct  = % of current position to liquidate
    """
    if current_position_value <= 0:
        return _no_trade("No position to sell")

    if target_price >= current_price:
        return _no_trade("target_price must be below current_price for SELL")

    reward = current_price - target_price   # how much we save by selling
    risk   = max(stop_loss - current_price, current_price * 0.03)  # upside risk

    rr = reward / risk if risk > 0 else 0
    if rr < MIN_RR_RATIO:
        return _no_trade(f"SELL R:R {rr:.2f} < {MIN_RR_RATIO} — hold for now")

    f_raw   = kelly_fraction(confidence, reward, risk)
    f_adj   = max(0.0, f_raw) * kelly_multiplier
    f_final = min(f_adj, max_position_pct)

    # f_final here = fraction of total portfolio; cap at full position
    dollar_amount = min(portfolio_value * f_final, current_position_value)
    liquidate_pct = dollar_amount / current_position_value * 100 if current_position_value > 0 else 0

    return {
        "dollar_amount":  round(dollar_amount, 2),
        "position_pct":   round(f_final * 100, 2),
        "liquidate_pct":  round(liquidate_pct, 1),
        "kelly_raw":      round(f_raw, 4),
        "kelly_adj":      round(f_adj, 4),
        "rr_ratio":       round(rr, 2),
        "reason":         (
            f"SELL Kelly({confidence:.0%}, {rr:.1f}x R:R) → "
            f"liquidate {liquidate_pct:.0f}% of position (${dollar_amount:,.2f})"
        ),
        "skip": False,
    }


def build_kelly_context(symbol: str, sizing: dict) -> str:
    """Format Kelly result for inclusion in AI analysis prompt."""
    if not sizing or sizing.get("skip"):
        return ""
    return (
        f"### 📐 KELLY CRITERION POSITION SIZING for {symbol}\n"
        f"Optimal allocation: {sizing['position_pct']:.1f}% of portfolio "
        f"(${sizing['dollar_amount']:,.2f})\n"
        f"Reward/Risk ratio: {sizing['rr_ratio']:.2f}x\n"
        f"Raw Kelly: {sizing['kelly_raw']:.1%} → Half-Kelly applied: {sizing['kelly_adj']:.1%}\n"
        f"→ Use this as your recommended_weight_pct in your response."
    )


def _no_trade(reason: str) -> dict:
    logger.debug(f"[Kelly] No trade: {reason}")
    return {
        "dollar_amount":  0.0,
        "position_pct":   0.0,
        "kelly_raw":      0.0,
        "kelly_adj":      0.0,
        "rr_ratio":       0.0,
        "reason":         reason,
        "skip":           True,
    }
