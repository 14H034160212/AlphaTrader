"""
Hong Kong IPO scanner — recent tech listings.

⚠️  RISK ACKNOWLEDGMENT
======================================================================
This module exists by user explicit request (option C, 2026-05-03):
"刚上市的港股科技股买入优先级最高"

This contradicts the long-term core/satellite policy because:
  • HK IPOs in their first 30 days routinely move ±25-50% on no news.
  • Buying just-listed names from the secondary market means absorbing
    institutional placement-tranche selling (structural disadvantage).
  • Multi-month underperformance vs Hang Seng is the historical norm
    for retail-grade HK tech IPOs (see 2024: 茶百道 -27% day-1,
    速腾聚创 -60% in 3 months).

Hard caps enforced (see auto_trade plumbing):
  • Per-name limit:  HK_IPO_MAX_NAME_PCT  (default 3% of equity)
  • Sector limit:    20% on the synthetic HK_IPO_NEW sector
  • Confidence:      same 0.75 minimum as the rest of the system

If user changes their mind, set DB setting `hk_ipo_priority_enabled=false`.
======================================================================
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Hard caps (enforced upstream in trading_engine.auto_trade via SYMBOL_SECTOR
# tagging). Adjust here if you want to widen/narrow the risk.
HK_IPO_LOOKBACK_DAYS    = 30   # treat anything listed within this window as "new"
HK_IPO_MAX_NAME_PCT     = 3.0  # max % of equity per single HK IPO position
HK_IPO_SECTOR_CAP_PCT   = 20.0 # max % of equity in HK_IPO_NEW sector total

# Tech sector keywords used to filter scraped IPO listings.
_TECH_KEYWORDS = (
    "tech", "technolog", "software", "internet", "AI", "artificial",
    "robot", "semiconductor", "chip", "biotech", "cloud", "saas",
    "platform", "data", "cyber", "fintech", "digital", "智能", "科技",
    "互联网", "软件", "云", "芯片", "半导体", "生物", "医疗", "数据",
)

# Known recent HK tech IPO seed list (kept current as fallback when scraping
# fails). Update when you spot new ones — better stale than empty since the
# list is the primary trade trigger source.
_SEED_RECENT_HK_TECH_IPOS: List[Dict] = [
    # Format: {"symbol": "xxxx.HK", "name": "...", "list_date": "YYYY-MM-DD"}
    # Empty by default — populated by scraper or manual user updates.
]


def _normalize_hk_ticker(raw: str) -> Optional[str]:
    """Convert various HK ticker formats to canonical 'NNNN.HK'."""
    if not raw:
        return None
    raw = raw.strip().upper().replace(" ", "")
    m = re.search(r"(\d{1,5})", raw)
    if not m:
        return None
    code = m.group(1).zfill(4)
    return f"{code}.HK"


def _is_tech_listing(name: str, sector: str = "") -> bool:
    haystack = f"{name} {sector}".lower()
    return any(kw.lower() in haystack for kw in _TECH_KEYWORDS)


def fetch_recent_hk_ipos_aastocks(lookback_days: int = HK_IPO_LOOKBACK_DAYS) -> List[Dict]:
    """
    Scrape AAStocks "recently listed" page for HK IPOs.
    Returns list of dicts: [{symbol, name, list_date, sector_raw}].

    The page is HTML-only (no API), so this is best-effort. Returns empty
    list on any error so callers can fall back to seed data.
    """
    url = "https://www.aastocks.com/en/stocks/market/ipo/upcomingipo/recent-listed"
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[HK_IPO] AAStocks fetch failed: {e}")
        return []

    html = resp.text
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=lookback_days)
    results: List[Dict] = []

    # AAStocks tables: each row has <td>NNNN</td><td>name</td>...<td>YYYY/MM/DD</td>
    # Use a permissive regex over rows; the page format changes occasionally.
    row_re = re.compile(
        r"<tr[^>]*>.*?(\d{4,5}).*?>([^<]{2,80})<.*?(\d{4}[/-]\d{2}[/-]\d{2}).*?</tr>",
        re.DOTALL,
    )
    for m in row_re.finditer(html):
        code, name_raw, date_str = m.group(1), m.group(2), m.group(3)
        try:
            list_date = datetime.strptime(
                date_str.replace("-", "/"), "%Y/%m/%d"
            ).date()
        except ValueError:
            continue
        # Only ALREADY-listed names (skip upcoming IPOs we can't trade yet)
        if list_date < cutoff or list_date > today:
            continue
        symbol = _normalize_hk_ticker(code)
        if not symbol:
            continue
        name = re.sub(r"\s+", " ", name_raw).strip()
        results.append({
            "symbol": symbol,
            "name": name,
            "list_date": list_date.isoformat(),
            "sector_raw": "",
        })

    logger.info(f"[HK_IPO] AAStocks scrape: {len(results)} recent HK listings")
    return results


def get_recent_hk_tech_ipos(lookback_days: int = HK_IPO_LOOKBACK_DAYS) -> List[Dict]:
    """
    Public entry: return recent HK tech IPOs (last `lookback_days`).
    Falls back to seed list when scraping yields nothing.
    """
    raw = fetch_recent_hk_ipos_aastocks(lookback_days)
    if not raw:
        raw = list(_SEED_RECENT_HK_TECH_IPOS)
    tech = [r for r in raw if _is_tech_listing(r.get("name", ""), r.get("sector_raw", ""))]
    # Dedupe on symbol; keep the most recent list_date entry per ticker
    by_sym: Dict[str, Dict] = {}
    for r in tech:
        s = r["symbol"]
        if s not in by_sym or r["list_date"] > by_sym[s]["list_date"]:
            by_sym[s] = r
    sorted_list = sorted(by_sym.values(), key=lambda x: x["list_date"], reverse=True)
    logger.info(f"[HK_IPO] {len(sorted_list)} recent HK tech IPOs after filter")
    return sorted_list


def is_hk_ipo_symbol(symbol: str, ipo_list: List[Dict]) -> bool:
    """True if `symbol` is in the current HK IPO watchlist."""
    if not symbol:
        return False
    s = symbol.upper().strip()
    return any(item.get("symbol", "").upper() == s for item in ipo_list)
