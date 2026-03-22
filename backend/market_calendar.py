"""
Global market calendar – trading hours, currencies, exchange detection.

All session times are defined in local exchange time (pytz handles DST).
Weekend detection uses UTC weekday (Mon=0 … Sun=6).
"""
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

try:
    import pytz
    _HAS_PYTZ = True
except ImportError:
    _HAS_PYTZ = False

# ── Timezone for each market code ────────────────────────────────────────────
MARKET_TIMEZONES: Dict[str, str] = {
    "US": "America/New_York",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "JP": "Asia/Tokyo",
    "GB": "Europe/London",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam",
    "IT": "Europe/Rome",
    "ES": "Europe/Madrid",
    "CH": "Europe/Zurich",
    "AU": "Australia/Sydney",
    "KR": "Asia/Seoul",
    "SG": "Asia/Singapore",
    "IN": "Asia/Kolkata",
    "BR": "America/Sao_Paulo",
    "CA": "America/Toronto",
    "MX": "America/Mexico_City",
    "RU": "Europe/Moscow",
    "ZA": "Africa/Johannesburg",
    "TW": "Asia/Taipei",
    "TH": "Asia/Bangkok",
    "MY": "Asia/Kuala_Lumpur",
    "ID": "Asia/Jakarta",
    "PH": "Asia/Manila",
    "VN": "Asia/Ho_Chi_Minh",
    "TR": "Europe/Istanbul",
    "SA": "Asia/Riyadh",
    "AE": "Asia/Dubai",
    "IL": "Asia/Jerusalem",
    "EG": "Africa/Cairo",
    "NG": "Africa/Lagos",
    "AR": "America/Argentina/Buenos_Aires",
    "CL": "America/Santiago",
    "CO": "America/Bogota",
    "PE": "America/Lima",
}

# ── Trading sessions: list of (open, close) in local time ────────────────────
# Markets with a lunch break have two sessions.
MARKET_SESSIONS: Dict[str, List[Tuple[time, time]]] = {
    "US": [(time(9, 30), time(16, 0))],
    "CN": [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))],
    "HK": [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))],
    "JP": [(time(9, 0), time(11, 30)), (time(12, 30), time(15, 30))],
    "GB": [(time(8, 0), time(16, 30))],
    "DE": [(time(9, 0), time(17, 30))],
    "FR": [(time(9, 0), time(17, 30))],
    "NL": [(time(9, 0), time(17, 30))],
    "IT": [(time(9, 0), time(17, 30))],
    "ES": [(time(9, 0), time(17, 30))],
    "CH": [(time(9, 0), time(17, 30))],
    "AU": [(time(10, 0), time(16, 0))],
    "KR": [(time(9, 0), time(15, 30))],
    "SG": [(time(9, 0), time(17, 0))],
    "IN": [(time(9, 15), time(15, 30))],
    "BR": [(time(10, 0), time(17, 55))],
    "CA": [(time(9, 30), time(16, 0))],
    "MX": [(time(8, 30), time(15, 0))],
    "RU": [(time(10, 0), time(18, 50))],
    "ZA": [(time(9, 0), time(17, 0))],
    "TW": [(time(9, 0), time(13, 30))],
    "TH": [(time(10, 0), time(12, 30)), (time(14, 30), time(16, 30))],
    "MY": [(time(9, 0), time(12, 30)), (time(14, 30), time(17, 0))],
    "TR": [(time(10, 0), time(18, 0))],
    "SA": [(time(10, 0), time(15, 0))],
    "AE": [(time(10, 0), time(14, 0))],
    "IL": [(time(9, 59), time(17, 14))],
    "AR": [(time(11, 0), time(17, 0))],
    "CL": [(time(9, 30), time(17, 30))],
}

# ── Currency for each market ──────────────────────────────────────────────────
MARKET_CURRENCIES: Dict[str, str] = {
    "US": "USD", "CN": "CNY", "HK": "HKD", "JP": "JPY",
    "GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR",
    "IT": "EUR", "ES": "EUR", "CH": "CHF", "AU": "AUD",
    "KR": "KRW", "SG": "SGD", "IN": "INR", "BR": "BRL",
    "CA": "CAD", "MX": "MXN", "RU": "RUB", "ZA": "ZAR",
    "TW": "TWD", "TH": "THB", "MY": "MYR", "TR": "TRY",
    "SA": "SAR", "AE": "AED", "IL": "ILS", "AR": "ARS",
    "CL": "CLP", "CO": "COP", "PE": "PEN",
    "ID": "IDR", "PH": "PHP", "VN": "VND", "EG": "EGP",
    "NG": "NGN",
}

# ── Market display names ──────────────────────────────────────────────────────
MARKET_NAMES: Dict[str, str] = {
    "US": "美股 (NYSE/NASDAQ)", "CN": "A股 (上交所/深交所)",
    "HK": "港股 (港交所)",     "JP": "日本 (东京证交所)",
    "GB": "英国 (伦敦证交所)", "DE": "德国 (法兰克福/XETRA)",
    "FR": "法国 (泛欧交易所)", "NL": "荷兰 (阿姆斯特丹)",
    "IT": "意大利 (米兰证交所)", "ES": "西班牙 (马德里)",
    "CH": "瑞士 (SIX)",       "AU": "澳大利亚 (ASX)",
    "KR": "韩国 (KRX)",        "SG": "新加坡 (SGX)",
    "IN": "印度 (NSE/BSE)",    "BR": "巴西 (B3/BOVESPA)",
    "CA": "加拿大 (TSX)",      "MX": "墨西哥 (BMV)",
    "TW": "台湾 (TWSE)",       "TH": "泰国 (SET)",
    "MY": "马来西亚 (Bursa)",  "TR": "土耳其 (BIST)",
    "SA": "沙特 (Tadawul)",    "AE": "阿联酋 (DFM/ADX)",
    "IL": "以色列 (TASE)",     "ZA": "南非 (JSE)",
    "AR": "阿根廷 (BCBA)",     "CL": "智利 (BCS)",
    "RU": "俄罗斯 (MOEX)",     "ID": "印尼 (IDX)",
    "PH": "菲律宾 (PSE)",      "VN": "越南 (HOSE)",
}

# ── Symbol suffix → market code ──────────────────────────────────────────────
_SUFFIX_TO_MARKET: Dict[str, str] = {
    # China A-shares
    "SH": "CN", "SS": "CN",   # Shanghai
    "SZ": "CN",                # Shenzhen
    # Hong Kong
    "HK": "HK",
    # Japan
    "T": "JP", "OS": "JP",
    # UK / Ireland
    "L": "GB",
    # Germany
    "DE": "DE", "F": "DE", "BE": "DE", "MU": "DE", "SG": "DE",
    # France
    "PA": "FR", "NX": "FR",
    # Netherlands
    "AS": "NL",
    # Italy
    "MI": "IT",
    # Spain
    "MC": "ES", "MA": "ES",
    # Switzerland
    "SW": "CH",
    # Australia
    "AX": "AU",
    # Korea
    "KS": "KR", "KQ": "KR",
    # Singapore
    "SI": "SG",
    # India
    "NS": "IN", "BO": "IN",
    # Brazil
    "SA": "BR",
    # Canada
    "TO": "CA", "V": "CA",
    # Mexico
    "MX": "MX",
    # Taiwan
    "TW": "TW", "TWO": "TW",
    # Thailand
    "BK": "TH",
    # Malaysia
    "KL": "MY",
    # Indonesia
    "JK": "ID",
    # Philippines
    "PS": "PH",
    # Russia
    "ME": "RU",
    # South Africa
    "JO": "ZA",
    # Israel
    "TA": "IL",
    # Turkey
    "IS": "TR",
    # Saudi Arabia
    "SR": "SA",
    # UAE
    "AE": "AE", "DU": "AE",
    # Argentina
    "BA": "AR",
    # Chile
    "SN": "CL",
    # Vietnam (not widely supported by yfinance)
    "VN": "VN",
}


def detect_market(symbol: str) -> str:
    """Detect market from symbol format. E.g. '600519.SH'→'CN', '0700.HK'→'HK', 'AAPL'→'US'."""
    if not symbol or "." not in symbol:
        return "US"
    suffix = symbol.rsplit(".", 1)[-1].upper()
    return _SUFFIX_TO_MARKET.get(suffix, "INTL")


def get_currency(symbol: str) -> str:
    """Return the trading currency for a symbol."""
    market = detect_market(symbol)
    return MARKET_CURRENCIES.get(market, "USD")


def is_market_open(market: str) -> bool:
    """
    Return True if the exchange for *market* is currently open.
    Falls back to UTC time + hardcoded UTC offsets when pytz is unavailable.
    """
    tz_name = MARKET_TIMEZONES.get(market)
    sessions = MARKET_SESSIONS.get(market)
    if not tz_name or not sessions:
        return False

    try:
        if _HAS_PYTZ:
            tz = pytz.timezone(tz_name)
            now_local = datetime.now(tz)
        else:
            # Rough UTC offset fallback
            _UTC_OFFSETS = {
                "America/New_York": -5, "Asia/Shanghai": 8, "Asia/Hong_Kong": 8,
                "Asia/Tokyo": 9, "Europe/London": 0, "Europe/Berlin": 1,
                "Europe/Paris": 1, "Europe/Amsterdam": 1, "Europe/Rome": 1,
                "Europe/Madrid": 1, "Europe/Zurich": 1, "Australia/Sydney": 11,
                "Asia/Seoul": 9, "Asia/Singapore": 8, "Asia/Kolkata": 5,
                "America/Sao_Paulo": -3, "America/Toronto": -5,
                "America/Mexico_City": -6, "Europe/Moscow": 3,
                "Africa/Johannesburg": 2, "Asia/Taipei": 8,
            }
            offset = _UTC_OFFSETS.get(tz_name, 0)
            from datetime import timezone, timedelta
            now_local = datetime.now(timezone(timedelta(hours=offset)))

        # Skip weekends (Sat=5, Sun=6)
        if now_local.weekday() >= 5:
            return False

        current_t = now_local.time().replace(tzinfo=None)
        for open_t, close_t in sessions:
            if open_t <= current_t <= close_t:
                return True
        return False
    except Exception:
        return False


def is_symbol_market_open(symbol: str) -> bool:
    """Convenience: is the market for *symbol* currently open?"""
    return is_market_open(detect_market(symbol))


def get_market_status(market: str) -> Dict:
    """Return status dict for a market including next open/close info."""
    open_now = is_market_open(market)
    tz_name = MARKET_TIMEZONES.get(market, "UTC")
    sessions = MARKET_SESSIONS.get(market, [])

    local_time_str = ""
    try:
        if _HAS_PYTZ:
            tz = pytz.timezone(tz_name)
            local_now = datetime.now(tz)
            local_time_str = local_now.strftime("%H:%M")
        else:
            local_time_str = "--:--"
    except Exception:
        pass

    return {
        "market": market,
        "name": MARKET_NAMES.get(market, market),
        "open": open_now,
        "currency": MARKET_CURRENCIES.get(market, "USD"),
        "timezone": tz_name,
        "local_time": local_time_str,
        "sessions": [
            {"open": s[0].strftime("%H:%M"), "close": s[1].strftime("%H:%M")}
            for s in sessions
        ],
    }


def get_all_market_statuses() -> Dict[str, Dict]:
    """Return status dict for all known markets."""
    return {market: get_market_status(market) for market in MARKET_TIMEZONES}


def get_market_open_count() -> Dict[str, int]:
    """Return count of currently open vs closed markets."""
    statuses = get_all_market_statuses()
    open_count = sum(1 for v in statuses.values() if v["open"])
    return {"open": open_count, "closed": len(statuses) - open_count, "total": len(statuses)}


# ── China-specific trading rules ──────────────────────────────────────────────

def is_china_ashare(symbol: str) -> bool:
    """True for Shanghai or Shenzhen A-shares."""
    if not symbol:
        return False
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    return suffix in ("SH", "SZ", "SS")


def is_hk_stock(symbol: str) -> bool:
    """True for Hong Kong Exchange stocks."""
    if not symbol:
        return False
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    return suffix == "HK"


def china_lot_size(symbol: str) -> int:
    """
    China A-share minimum lot size (手数).
    Regular board: 100 shares.
    STAR Market (688xxx) and ChiNext (300xxx): 200 shares for first purchase.
    For simplicity, return 100 as the universal minimum.
    """
    return 100


def round_to_lot(quantity: float, symbol: str) -> int:
    """Round quantity down to nearest lot size for A-shares."""
    lot = china_lot_size(symbol)
    return max(lot, int(quantity / lot) * lot)


def check_china_price_limit(symbol: str, current_price: float, prev_close: float) -> Dict:
    """
    Check if an A-share has hit its daily price limit.
    Regular board: ±10%. ST/ST* stocks: ±5%.
    Returns: {hit_limit: bool, direction: 'UP'|'DOWN'|None, limit_pct: float}
    """
    if not is_china_ashare(symbol):
        return {"hit_limit": False, "direction": None, "limit_pct": 0.0}

    code = symbol.split(".")[0]
    # ST stocks start with 'ST' or '*ST' in name – we can only detect via code pattern
    # 600xxx, 000xxx, 300xxx = normal ±10%; 688xxx (STAR) = ±20%
    if code.startswith("688"):
        limit_pct = 20.0  # STAR Market
    else:
        limit_pct = 10.0  # Default (ST stocks would be ±5% but we can't detect without name)

    if prev_close <= 0:
        return {"hit_limit": False, "direction": None, "limit_pct": limit_pct}

    change_pct = (current_price - prev_close) / prev_close * 100
    if change_pct >= limit_pct - 0.01:
        return {"hit_limit": True, "direction": "UP", "limit_pct": limit_pct}
    if change_pct <= -(limit_pct - 0.01):
        return {"hit_limit": True, "direction": "DOWN", "limit_pct": limit_pct}
    return {"hit_limit": False, "direction": None, "limit_pct": limit_pct}
