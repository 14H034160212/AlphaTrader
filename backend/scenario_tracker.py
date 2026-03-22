"""
Scenario Performance Tracker — 场景表现追踪器

Replaces rigid time-based rules ("skip after 7 days") with
evidence-based assessment: "are the beneficiary stocks actually
moving in the predicted direction?"

Core concept:
  - When a macro scenario (e.g., Middle East War) first triggers a trade,
    record the entry prices of all beneficiary stocks.
  - On each subsequent scan, compute how much those stocks have moved
    since the first trade.
  - Report a health score the AI can use to decide position size.

Health status:
  WORKING  : avg beneficiary performance > +3%   → thesis confirmed, normal sizing
  MIXED    : avg between -3% and +3%             → uncertain, reduce size
  FAILING  : avg < -3%                           → thesis not playing out, minimal size
  UNKNOWN  : no previous trades found            → treat as fresh, normal sizing
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_scenario_health(
    scenario_name: str,
    beneficiaries: List[str],
    db: Session,
    user_id: int,
    price_cache: dict,
) -> dict:
    """
    Assess how well a macro scenario thesis has played out based on actual
    price movement of its beneficiary stocks since the first trade.

    Returns:
        status        : "working" | "mixed" | "failing" | "unknown"
        avg_pct       : float — average % change of beneficiaries since entry
        days_active   : int
        position_mult : float — suggested position size multiplier (0.2 – 1.0)
        context_str   : str   — plain-text block to inject into AI prompt
    """
    from database import Trade  # late import to avoid circular

    # Use first ~20 chars of name to search reasoning text
    name_tag = scenario_name[:25]

    first_trade = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.reasoning.like(f"%{name_tag}%"),
            Trade.side == "BUY",
        )
        .order_by(Trade.timestamp.asc())
        .first()
    )

    if not first_trade:
        return {
            "status": "unknown",
            "avg_pct": 0.0,
            "days_active": 0,
            "position_mult": 1.0,
            "context_str": "",
        }

    days_active = (datetime.utcnow() - first_trade.timestamp).days

    perfs: List[float] = []
    per_stock_lines: List[str] = []

    for sym in beneficiaries:
        sym_trade = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.symbol == sym,
                Trade.reasoning.like(f"%{name_tag}%"),
                Trade.side == "BUY",
            )
            .order_by(Trade.timestamp.asc())
            .first()
        )
        if not sym_trade:
            continue
        entry_price = float(sym_trade.price or 0)
        current = (price_cache.get(sym) or {}).get("current", 0)
        if entry_price > 0 and current > 0:
            pct = (current / entry_price - 1) * 100
            perfs.append(pct)
            per_stock_lines.append(
                f"{sym}: entry=${entry_price:.2f} now=${current:.2f} ({pct:+.1f}%)"
            )

    avg_pct = sum(perfs) / len(perfs) if perfs else 0.0

    # Determine health status and position multiplier
    if not perfs:
        status, pos_mult = "unknown", 1.0
    elif avg_pct >= 3.0:
        status, pos_mult = "working", 1.0
    elif avg_pct >= -3.0:
        status, pos_mult = "mixed", 0.6
    else:
        status, pos_mult = "failing", 0.3

    # Build AI-readable context block
    lines = [
        f"### SCENARIO PERFORMANCE TRACKER: '{scenario_name[:50]}'",
        f"Active {days_active} days | Avg beneficiary P&L since entry: {avg_pct:+.1f}% | Status: {status.upper()}",
    ]
    if per_stock_lines:
        lines.append("Per-stock since first trade: " + " | ".join(per_stock_lines))

    if status == "working":
        lines.append(
            "INTERPRETATION: Thesis confirmed by price action. "
            "Normal position sizing is appropriate."
        )
    elif status == "mixed":
        lines.append(
            "INTERPRETATION: Thesis is inconclusive — some beneficiaries up, some down. "
            "Use reduced position sizing and require stronger confirmation before BUY."
        )
    elif status == "failing":
        lines.append(
            "INTERPRETATION: Thesis is NOT playing out — beneficiaries are DOWN since scenario start. "
            "This suggests the market has either priced-in the news or the narrative has reversed. "
            "Do NOT add to losing positions without strong new catalysts. "
            "Consider whether fresh positive evidence exists before any new BUY."
        )

    return {
        "status": status,
        "avg_pct": round(avg_pct, 2),
        "days_active": days_active,
        "position_mult": pos_mult,
        "context_str": "\n".join(lines),
    }


def build_scenario_health_context(
    macros: list,
    db: Session,
    user_id: int,
    price_cache: dict,
) -> str:
    """
    Build combined scenario health context string for a list of active macros.
    Used to inject into AI analysis prompts.
    """
    parts = []
    for macro in macros:
        health = get_scenario_health(
            macro.get("name", ""),
            macro.get("potential_beneficiaries", []),
            db,
            user_id,
            price_cache,
        )
        if health["context_str"]:
            parts.append(health["context_str"])
    return "\n\n".join(parts)
