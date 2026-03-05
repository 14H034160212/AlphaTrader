"""
COT (Commitments of Traders) Data — 期货对手盘
CFTC publishes free weekly COT reports every Friday (positions as of Tuesday).
Source: https://www.cftc.gov/dea/newcot/deacot.zip  (no API key required)

Tells us whether large speculators (hedge funds / momentum) and commercials
(hedgers, smart money) are net long or short on key futures contracts.

Mapping: futures contract → affected stock tickers
  Gold   → GLD, IAU
  Silver → SLV
  S&P500 → SPY, QQQ
  Nasdaq → QQQ, TQQQ, NVDA, AAPL, MSFT
  Crude  → XOM
  Bitcoin → IBIT, MSTR, COIN
"""

import io
import os
import logging
import zipfile
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_COT_URL = "https://www.cftc.gov/sites/default/files/files/dea/history/deacot{year}.zip"
_CACHE_FILE = "/tmp/alphatrader_cot.pkl"
_CACHE_TTL_HOURS = 48  # refresh every 2 days (report is weekly)

# ── Futures contract name substrings → stock tickers ─────────────────────────
# Keys are substrings to search in the "Market_and_Exchange_Names" column.
# Ordered by priority (most specific first).
FUTURES_TICKER_MAP = {
    "GOLD":            ["GLD", "IAU"],
    "SILVER":          ["SLV"],
    "E-MINI S&P 500":  ["SPY", "QQQ"],
    "NASDAQ-100":      ["QQQ", "TQQQ", "NVDA", "AAPL", "MSFT"],
    "BITCOIN":         ["IBIT", "MSTR", "COIN"],
    "CRUDE OIL":       ["XOM"],
    "NATURAL GAS":     ["XOM"],
    "EURO FX":         [],         # FX — not directly mapped to equities
    "JAPANESE YEN":    [],
    "COPPER":          ["FCX"],
}

# Human-readable labels for display
CONTRACT_LABELS = {
    "GOLD":           "黄金期货",
    "SILVER":         "白银期货",
    "E-MINI S&P 500": "标普500期货(ES)",
    "NASDAQ-100":     "纳指100期货(NQ)",
    "BITCOIN":        "比特币期货(CME)",
    "CRUDE OIL":      "轻质原油期货(CL)",
    "NATURAL GAS":    "天然气期货(NG)",
}


# ── Column name constants (CFTC legacy format, actual header names) ───────────
_COL_NAME   = "Market and Exchange Names"
_COL_DATE   = "As of Date in Form YYMMDD"
_COL_NC_L   = "Noncommercial Positions-Long (All)"
_COL_NC_S   = "Noncommercial Positions-Short (All)"
_COL_CM_L   = "Commercial Positions-Long (All)"
_COL_CM_S   = "Commercial Positions-Short (All)"
_COL_OI     = "Open Interest (All)"


def _load_cot_df() -> pd.DataFrame | None:
    """Download and parse the CFTC legacy COT CSV. Uses a local parquet cache."""
    # Check cache freshness
    if os.path.exists(_CACHE_FILE):
        mtime = datetime.utcfromtimestamp(os.path.getmtime(_CACHE_FILE))
        if datetime.utcnow() - mtime < timedelta(hours=_CACHE_TTL_HOURS):
            try:
                return pd.read_pickle(_CACHE_FILE)
            except Exception:
                pass

    try:
        # Try current year first, fall back to previous year
        year = datetime.utcnow().year
        url = _COT_URL.format(year=year)
        logger.info(f"[COT] Downloading CFTC COT data from {url}")
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "AlphaTrader-COT/1.0"})
        if resp.status_code == 404:
            url = _COT_URL.format(year=year - 1)
            logger.info(f"[COT] Trying previous year: {url}")
            resp = requests.get(url, timeout=30,
                                headers={"User-Agent": "AlphaTrader-COT/1.0"})
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            # The archive contains 'deacot.txt'
            fname = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not fname:
                logger.error("[COT] No .txt file found in deacot.zip")
                return None
            with z.open(fname) as f:
                df = pd.read_csv(f, low_memory=False)

        # Use the YYYY-MM-DD column for reliable parsing; drop the YYMMDD one first
        yymmdd_col = "As of Date in Form YYMMDD"
        alt_date = "As of Date in Form YYYY-MM-DD"
        if alt_date in df.columns:
            if yymmdd_col in df.columns:
                df = df.drop(columns=[yymmdd_col])
            df = df.rename(columns={alt_date: _COL_DATE})
        keep = [_COL_NAME, _COL_DATE, _COL_NC_L, _COL_NC_S,
                _COL_CM_L, _COL_CM_S, _COL_OI]
        missing = [c for c in keep if c not in df.columns]
        if missing:
            logger.warning(f"[COT] Missing columns: {missing}. Available: {list(df.columns[:12])}")
            return None

        df = df[keep].copy()
        df[_COL_DATE] = pd.to_datetime(df[_COL_DATE], errors="coerce")
        for col in [_COL_NC_L, _COL_NC_S, _COL_CM_L, _COL_CM_S, _COL_OI]:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")

        df = df.dropna(subset=[_COL_DATE]).sort_values(_COL_DATE)

        try:
            df.to_pickle(_CACHE_FILE)
            logger.info(f"[COT] Cached {len(df)} COT rows to {_CACHE_FILE}")
        except Exception as e:
            logger.warning(f"[COT] Cache write failed: {e}")

        return df

    except Exception as e:
        logger.error(f"[COT] Download/parse error: {e}")
        return None


def _get_latest_row(df: pd.DataFrame, keyword: str) -> pd.Series | None:
    """Return the most recent row whose contract name contains `keyword`."""
    mask = df[_COL_NAME].str.upper().str.contains(keyword.upper(), na=False)
    subset = df[mask]
    if subset.empty:
        return None
    return subset.sort_values(_COL_DATE).iloc[-1]


def _net_spec_pct(row: pd.Series) -> float:
    """Large speculator net long % of open interest. Positive = net long (bullish sentiment)."""
    oi = row[_COL_OI]
    if not oi or oi == 0:
        return 0.0
    net = row[_COL_NC_L] - row[_COL_NC_S]
    return round(net / oi * 100, 1)


def _net_comm_pct(row: pd.Series) -> float:
    """Commercial (smart money / hedgers) net long % of OI. Contrarian indicator."""
    oi = row[_COL_OI]
    if not oi or oi == 0:
        return 0.0
    net = row[_COL_CM_L] - row[_COL_CM_S]
    return round(net / oi * 100, 1)


def _bias_label(spec_net_pct: float) -> str:
    if spec_net_pct > 20:
        return "STRONG LONG"
    elif spec_net_pct > 5:
        return "NET LONG"
    elif spec_net_pct < -20:
        return "STRONG SHORT"
    elif spec_net_pct < -5:
        return "NET SHORT"
    return "NEUTRAL"


# ── Public API ────────────────────────────────────────────────────────────────

def get_cot_for_symbol(symbol: str) -> dict | None:
    """
    Return COT positioning data for the futures contract most relevant to `symbol`.
    Returns dict with: contract, report_date, spec_net_pct, comm_net_pct, bias, open_interest
    Returns None if no mapping found or data unavailable.
    """
    # Find which futures keywords map to this symbol
    matched_key = None
    for keyword, tickers in FUTURES_TICKER_MAP.items():
        if symbol in tickers:
            matched_key = keyword
            break
    if matched_key is None:
        return None

    df = _load_cot_df()
    if df is None:
        return None

    row = _get_latest_row(df, matched_key)
    if row is None:
        logger.warning(f"[COT] No row found for keyword '{matched_key}'")
        return None

    spec_net = _net_spec_pct(row)
    comm_net = _net_comm_pct(row)

    return {
        "contract":       CONTRACT_LABELS.get(matched_key, matched_key),
        "report_date":    row[_COL_DATE].strftime("%Y-%m-%d") if pd.notna(row[_COL_DATE]) else "unknown",
        "spec_net_pct":   spec_net,     # speculators net long % of OI
        "comm_net_pct":   comm_net,     # commercials net long % (contrarian)
        "spec_long":      int(row[_COL_NC_L]),
        "spec_short":     int(row[_COL_NC_S]),
        "comm_long":      int(row[_COL_CM_L]),
        "comm_short":     int(row[_COL_CM_S]),
        "open_interest":  int(row[_COL_OI]),
        "bias":           _bias_label(spec_net),
    }


def build_cot_context(symbol: str) -> str:
    """
    Return a formatted string for inclusion in the AI analysis prompt.
    Explains the current futures positioning and its implication.
    """
    cot = get_cot_for_symbol(symbol)
    if not cot:
        return ""

    spec_arrow = "↑" if cot["spec_net_pct"] > 0 else "↓"
    comm_arrow = "↑" if cot["comm_net_pct"] > 0 else "↓"

    # Interpret commercial positioning (contrarian): commercials net short = they are hedging
    # their physical exposure → bearish for price. Commercials net long = unusual → bullish.
    comm_interpretation = (
        "Commercials (hedgers) are unusually NET LONG — historically bullish divergence."
        if cot["comm_net_pct"] > 5 else
        "Commercials (hedgers) are net short — normal hedging behavior."
        if cot["comm_net_pct"] < -5 else
        "Commercials are roughly neutral."
    )

    lines = [
        f"### 📊 FUTURES COT POSITIONING for {symbol} ({cot['contract']})",
        f"Report date: {cot['report_date']}",
        f"",
        f"Large Speculators (momentum funds):  {spec_arrow} {cot['bias']} "
        f"({cot['spec_net_pct']:+.1f}% of OI | "
        f"Long {cot['spec_long']:,} / Short {cot['spec_short']:,})",
        f"Commercials (hedgers / smart money): {comm_arrow} "
        f"({cot['comm_net_pct']:+.1f}% of OI | "
        f"Long {cot['comm_long']:,} / Short {cot['comm_short']:,})",
        f"Total Open Interest: {cot['open_interest']:,}",
        f"",
        f"Interpretation: {comm_interpretation}",
        f"→ Spec bias is {cot['bias']}. Factor this into your conviction level for {symbol}.",
    ]
    return "\n".join(lines)


def get_all_cot_summary() -> list[dict]:
    """
    Return COT summary for all mapped contracts.
    Useful for the daily email report.
    """
    df = _load_cot_df()
    if df is None:
        return []

    results = []
    for keyword, tickers in FUTURES_TICKER_MAP.items():
        if not tickers:
            continue
        row = _get_latest_row(df, keyword)
        if row is None:
            continue
        spec_net = _net_spec_pct(row)
        results.append({
            "contract": CONTRACT_LABELS.get(keyword, keyword),
            "tickers": tickers,
            "bias": _bias_label(spec_net),
            "spec_net_pct": spec_net,
            "comm_net_pct": _net_comm_pct(row),
            "report_date": row[_COL_DATE].strftime("%Y-%m-%d") if pd.notna(row[_COL_DATE]) else "unknown",
        })
    return results
