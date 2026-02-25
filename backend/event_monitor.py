"""
Event Monitor - Forward-looking intelligence for event-driven trading.
Collects upcoming earnings, Fed meetings, economic releases and builds
context for AI to position BEFORE events are announced.
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

import logging
from datetime import datetime, timedelta
import yfinance as yf

logger = logging.getLogger(__name__)

# ── 2026 FOMC Meeting Dates (Fed announces rate decisions) ──────────────────
# Markets typically move 1-3 days BEFORE these dates based on expectations.
# Gold & bonds move on rate expectations; tech moves inversely to rates.
FOMC_DATES = [
    "2026-01-29",  # Jan meeting (past)
    "2026-03-18",  # Mar meeting
    "2026-05-07",  # May meeting
    "2026-06-17",  # Jun meeting
    "2026-07-29",  # Jul meeting
    "2026-09-16",  # Sep meeting
    "2026-11-04",  # Nov meeting
    "2026-12-16",  # Dec meeting
]

# ── Key Economic Data Release Dates 2026 ────────────────────────────────────
# CPI (Consumer Price Index) - high CPI = hawkish Fed = bad for tech/gold short-term
# NFP (Non-Farm Payrolls) - strong jobs = hawkish = bad for bonds/gold
# PCE (Fed's preferred inflation metric) - released last Friday of month
ECONOMIC_EVENTS = [
    # Format: (date, name, expected_impact, affected_assets)
    ("2026-02-12", "CPI Release", "HIGH", "GLD,SLV,TLT,QQQ,SPY"),
    ("2026-02-27", "PCE Inflation", "HIGH", "GLD,SLV,QQQ,SPY"),
    ("2026-03-06", "NFP Jobs Report", "HIGH", "SPY,QQQ,GLD"),
    ("2026-03-12", "CPI Release", "HIGH", "GLD,SLV,TLT,QQQ,SPY"),
    ("2026-03-27", "PCE Inflation", "HIGH", "GLD,SLV,QQQ,SPY"),
    ("2026-04-03", "NFP Jobs Report", "HIGH", "SPY,QQQ,GLD"),
    ("2026-04-10", "CPI Release", "HIGH", "GLD,SLV,TLT,QQQ,SPY"),
    ("2026-04-30", "PCE Inflation", "HIGH", "GLD,SLV,QQQ,SPY"),
    ("2026-05-08", "NFP Jobs Report", "HIGH", "SPY,QQQ,GLD"),
    ("2026-05-13", "CPI Release", "HIGH", "GLD,SLV,TLT,QQQ,SPY"),
    ("2026-05-29", "PCE Inflation", "HIGH", "GLD,SLV,QQQ,SPY"),
    ("2026-06-05", "NFP Jobs Report", "HIGH", "SPY,QQQ,GLD"),
    ("2026-06-10", "CPI Release", "HIGH", "GLD,SLV,TLT,QQQ,SPY"),
]


def get_upcoming_fomc(days_ahead=14):
    """Return FOMC meetings within the next N days."""
    today = datetime.now().date()
    upcoming = []
    for date_str in FOMC_DATES:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        days_until = (event_date - today).days
        if 0 <= days_until <= days_ahead:
            # Determine likely impact based on recent context
            upcoming.append({
                "event": "FOMC Fed Rate Decision",
                "date": date_str,
                "days_until": days_until,
                "urgency": "CRITICAL" if days_until <= 2 else "HIGH",
                "trading_implication": (
                    "Position 1-2 days before. If rate cut expected: BUY GLD, IAU, QQQ, tech. "
                    "If rate hike expected: SELL tech, BUY financials (JPM, V). "
                    "Gold (GLD/IAU/SLV) typically rallies on dovish signals."
                ),
                "affected_assets": ["GLD", "IAU", "SLV", "QQQ", "SPY", "TQQQ", "JPM"]
            })
    return upcoming


def get_upcoming_economic_events(days_ahead=7):
    """Return key economic data releases within the next N days."""
    today = datetime.now().date()
    upcoming = []
    for date_str, name, impact, assets in ECONOMIC_EVENTS:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        days_until = (event_date - today).days
        if 0 <= days_until <= days_ahead:
            upcoming.append({
                "event": name,
                "date": date_str,
                "days_until": days_until,
                "impact": impact,
                "urgency": "CRITICAL" if days_until <= 1 else "HIGH",
                "affected_assets": assets.split(","),
                "trading_implication": (
                    "CPI/PCE above expectations → hawkish Fed risk → "
                    "SELL tech (QQQ/TQQQ), gold may dip short-term then recover. "
                    "CPI/PCE below expectations → dovish → BUY GLD, IAU, SLV, QQQ. "
                    "NFP strong jobs → hawkish → SELL bonds/gold short-term."
                )
            })
    return upcoming


def get_upcoming_earnings(symbols, days_ahead=7):
    """Fetch upcoming earnings dates for watchlist symbols via yfinance."""
    today = datetime.now().date()
    upcoming = []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            if cal is None:
                continue
            # yfinance returns calendar as dict or DataFrame depending on version
            if hasattr(cal, 'empty') and cal.empty:
                continue
            # Try to extract earnings date
            if isinstance(cal, dict):
                earnings_date = cal.get("Earnings Date")
            else:
                try:
                    earnings_date = cal.loc["Earnings Date"].iloc[0] if "Earnings Date" in cal.index else None
                except Exception:
                    earnings_date = None

            if earnings_date is None:
                continue

            if hasattr(earnings_date, 'date'):
                earnings_date = earnings_date.date()
            elif isinstance(earnings_date, str):
                earnings_date = datetime.strptime(earnings_date[:10], "%Y-%m-%d").date()

            days_until = (earnings_date - today).days
            if 0 <= days_until <= days_ahead:
                # Fetch analyst estimates for context
                info = ticker.info
                eps_est = info.get("forwardEps", "N/A")
                rev_est = info.get("revenueEstimates", "N/A")
                upcoming.append({
                    "symbol": symbol,
                    "event": "Earnings Release",
                    "date": str(earnings_date),
                    "days_until": days_until,
                    "urgency": "CRITICAL" if days_until <= 1 else ("HIGH" if days_until <= 3 else "MEDIUM"),
                    "eps_estimate": eps_est,
                    "trading_implication": (
                        f"{symbol} reports earnings in {days_until} day(s). "
                        "AI should assess: beat probability based on recent guidance, "
                        "sector momentum, and short interest. "
                        "Position 1-2 days BEFORE earnings if high conviction."
                    )
                })
        except Exception as e:
            logger.debug(f"Could not get earnings for {symbol}: {e}")
    return upcoming


def build_event_context(symbols, days_ahead=7):
    """
    Build a comprehensive forward-looking event context string for the AI.
    This is injected into every AI analysis prompt so the AI knows
    what's coming and can position BEFORE announcements.
    """
    fomc = get_upcoming_fomc(days_ahead=14)
    econ = get_upcoming_economic_events(days_ahead)
    earnings = get_upcoming_earnings(symbols, days_ahead)

    lines = ["### UPCOMING MARKET EVENTS (Position BEFORE these dates)"]

    if fomc:
        lines.append("\n🏛️ FED / CENTRAL BANK:")
        for e in sorted(fomc, key=lambda x: x["days_until"]):
            lines.append(
                f"  [{e['urgency']}] {e['event']} — {e['date']} "
                f"({e['days_until']}d away)\n"
                f"  → {e['trading_implication']}"
            )

    if econ:
        lines.append("\n📊 ECONOMIC DATA RELEASES:")
        for e in sorted(econ, key=lambda x: x["days_until"]):
            lines.append(
                f"  [{e['urgency']}] {e['event']} — {e['date']} "
                f"({e['days_until']}d away) | Affects: {', '.join(e['affected_assets'])}\n"
                f"  → {e['trading_implication']}"
            )

    if earnings:
        lines.append("\n📈 EARNINGS RELEASES:")
        for e in sorted(earnings, key=lambda x: x["days_until"]):
            lines.append(
                f"  [{e['urgency']}] {e['symbol']} Earnings — {e['date']} "
                f"({e['days_until']}d away) | EPS Est: {e['eps_estimate']}\n"
                f"  → {e['trading_implication']}"
            )

    if len(lines) == 1:
        lines.append("  No major scheduled events in the next 7 days.")

    lines.append(
        "\n⚡ INSTRUCTION: Use the above events to ANTICIPATE price movements. "
        "If a positive catalyst is imminent (dovish Fed, earnings beat expected, low CPI), "
        "recommend BUY now to capture the pre-event run-up. "
        "If negative catalyst is imminent, recommend SELL existing positions."
    )

    return "\n".join(lines)


def get_event_priority_symbols(watchlist, days_ahead=3):
    """
    Return symbols that have imminent events and should be analyzed first.
    Used by the pre-event scan to prioritize which stocks to analyze.
    """
    priority = set()

    # Symbols affected by FOMC
    fomc = get_upcoming_fomc(days_ahead)
    if fomc:
        for e in fomc:
            priority.update(e["affected_assets"])

    # Symbols affected by economic releases
    econ = get_upcoming_economic_events(days_ahead)
    if econ:
        for e in econ:
            priority.update(e["affected_assets"])

    # Symbols with upcoming earnings
    earnings = get_upcoming_earnings(watchlist, days_ahead)
    if earnings:
        for e in earnings:
            priority.add(e["symbol"])

    # Filter to only include symbols actually in the watchlist
    return [s for s in watchlist if s in priority]
