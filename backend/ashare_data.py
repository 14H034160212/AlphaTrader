"""
Chinese A-share market data via Sina Finance APIs (free, no API key).
  Real-time:  https://hq.sinajs.cn/list=sh600519
  History:    https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/
              CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=132
"""
import re
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_SINA_RT_URL = "https://hq.sinajs.cn/list={symbols}"
_SINA_HIST_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}"
)
_HTTP_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

_ASHARE_RE = re.compile(r"^\d{6}\.(SH|SZ)$", re.IGNORECASE)


def is_ashare_symbol(symbol: str) -> bool:
    """Return True if symbol is a Chinese A-share (e.g. '600519.SH', '000001.SZ')."""
    return bool(_ASHARE_RE.match(symbol or ""))


def normalize_code(symbol: str) -> str:
    """Strip exchange suffix → 6-digit code ('600519.SH' → '600519')."""
    return symbol.split(".")[0]


def _infer_exchange(code: str) -> str:
    """6xxxxx → SH (Shanghai), others → SZ (Shenzhen)."""
    return "SH" if code.startswith("6") else "SZ"


def _sina_symbol(code: str) -> str:
    """Convert 6-digit code to Sina exchange prefix ('600519' → 'sh600519')."""
    return ("sh" if code.startswith("6") else "sz") + code


# ---------------------------------------------------------------------------
# Period → bar count
# ---------------------------------------------------------------------------
_PERIOD_BARS = {
    "1mo": 22,
    "3mo": 66,
    "6mo": 132,
    "1y": 252,
    "2y": 504,
    "5y": 1260,
}


# ---------------------------------------------------------------------------
# History (OHLCV)
# ---------------------------------------------------------------------------

def _fetch_history_df(code: str, period: str = "6mo") -> pd.DataFrame | None:
    """Fetch adjusted daily OHLCV from Sina Finance as a normalised DataFrame."""
    try:
        n_bars = _PERIOD_BARS.get(period, 132)
        # Add buffer for weekends/holidays
        n_fetch = int(n_bars * 1.5)
        url = _SINA_HIST_URL.format(symbol=_sina_symbol(code), n=n_fetch)
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None

        df = pd.DataFrame(data)
        df = df.rename(columns={
            "day": "date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "volume",
        })
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"[AShare] Error fetching history for {code}: {e}")
        return None


def get_ashare_history(symbol: str, period: str = "6mo", interval: str = "1d") -> list:
    """
    Return OHLCV list compatible with market_data.get_stock_history().
    'time' is a Unix timestamp integer (for Lightweight Charts).
    """
    code = normalize_code(symbol)
    df = _fetch_history_df(code, period)
    if df is None or df.empty:
        return []

    n_bars = _PERIOD_BARS.get(period, 132)
    df = df.tail(n_bars)

    result = []
    for _, row in df.iterrows():
        result.append({
            "time": int(row["date"].timestamp()),
            "open": round(float(row["open"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "close": round(float(row["close"]), 4),
            "volume": int(row["volume"]) if not pd.isna(row["volume"]) else 0,
        })
    return result


# ---------------------------------------------------------------------------
# Real-time quote
# ---------------------------------------------------------------------------

def _fetch_rt_quote(code: str) -> dict | None:
    """
    Fetch real-time quote from Sina Finance.
    Response format (comma-separated):
      name,open,prev_close,current,high,low,buy1,sell1,volume(手),amount,...
    """
    try:
        sina_sym = _sina_symbol(code)
        url = _SINA_RT_URL.format(symbols=sina_sym)
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=10)
        resp.raise_for_status()
        text = resp.text

        # Extract: var hq_str_shXXXXXX="field1,field2,...";
        match = re.search(r'="([^"]+)"', text)
        if not match:
            return None
        parts = match.group(1).split(",")
        if len(parts) < 10:
            return None

        name = parts[0]
        open_ = float(parts[1]) if parts[1] else 0
        prev_close = float(parts[2]) if parts[2] else 0
        current = float(parts[3]) if parts[3] else 0
        high = float(parts[4]) if parts[4] else 0
        low = float(parts[5]) if parts[5] else 0
        # volume in 手 (100 shares each), amount in 元
        volume = int(float(parts[8])) if parts[8] else 0
        return {
            "name": name,
            "open": open_,
            "prev_close": prev_close,
            "current": current,
            "high": high,
            "low": low,
            "volume": volume * 100,  # convert 手 → shares
        }
    except Exception as e:
        logger.error(f"[AShare] Error fetching RT quote for {code}: {e}")
        return None


def get_ashare_quote(symbol: str) -> dict | None:
    """
    Return a quote dict compatible with market_data.get_stock_quote().
    Uses Sina Finance real-time API + daily history for 52W range.
    """
    try:
        code = normalize_code(symbol)

        # Real-time price (fast single-stock call)
        rt = _fetch_rt_quote(code)

        # Fallback to history-based price if RT is unavailable/zero
        hist = _fetch_history_df(code, "3mo")
        if (rt is None or rt["current"] == 0) and (hist is None or hist.empty):
            logger.warning(f"[AShare] No data for {symbol}")
            return None

        if rt and rt["current"] != 0:
            current = rt["current"]
            prev_close = rt["prev_close"]
            open_ = rt["open"]
            high = rt["high"]
            low = rt["low"]
            volume = rt["volume"]
            name = rt["name"] or symbol
        else:
            # Market closed — use last available close
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else last
            current = float(last["close"])
            prev_close = float(prev["close"])
            open_ = float(last["open"])
            high = float(last["high"])
            low = float(last["low"])
            volume = int(last["volume"]) if not pd.isna(last["volume"]) else 0
            name = symbol

        change = current - prev_close
        change_pct = (change / prev_close * 100) if prev_close != 0 else 0

        # 52-week high/low from history
        w52_high, w52_low = None, None
        if hist is not None and not hist.empty:
            w52_high = round(float(hist["high"].max()), 2)
            w52_low = round(float(hist["low"].min()), 2)

        # VPA analysis
        vpa_signal, crowding, liquidity, vol_ratio = "NEUTRAL", 0.5, 0.5, 1.0
        if hist is not None and len(hist) >= 5:
            hist_records = [
                {"open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["volume"])}
                for _, r in hist.tail(20).iterrows()
            ]
            try:
                from quant_models import QuantitativeModels
                vpa = QuantitativeModels.analyze_volume_price_action(hist_records)
                vpa_signal = vpa["vpa_signal"]
                crowding = vpa["crowding"]
                liquidity = vpa["liquidity"]
                vol_ratio = vpa["volume_ratio"]
            except Exception:
                pass

        return {
            "symbol": symbol,
            "name": name,
            "current": round(current, 2),
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "volume": volume,
            "change": round(change, 2),
            "change_pct": round(change_pct, 3),
            "market_cap": None,
            "pe_ratio": None,
            "fifty_two_week_high": w52_high,
            "fifty_two_week_low": w52_low,
            "sector": "A股",
            "currency": "CNY",
            "exchange": _infer_exchange(code),
            "timestamp": datetime.utcnow().isoformat(),
            "dcf_value": 0.0,
            "ddm_value": 0.0,
            "intrinsic_value": 0.0,
            "valuation_gap_pct": 0.0,
            "vpa_signal": vpa_signal,
            "crowding": crowding,
            "liquidity": liquidity,
            "vpa_volume_ratio": vol_ratio,
        }
    except Exception as e:
        logger.error(f"[AShare] Error fetching quote for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def get_ashare_indicators(symbol: str) -> dict:
    """
    Calculate technical indicators for an A-share.
    Output format matches market_data.get_technical_indicators().
    """
    code = normalize_code(symbol)
    df = _fetch_history_df(code, "6mo")
    if df is None or df.empty or len(df) < 20:
        return {}

    closes = df["close"]
    volumes = df["volume"]

    try:
        ma20 = float(closes.rolling(20).mean().iloc[-1])
        ma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
        ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None

        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])

        ema12 = closes.ewm(span=12).mean()
        ema26 = closes.ewm(span=26).mean()
        macd_line = ema12 - ema26
        macd = float(macd_line.iloc[-1])
        macd_signal = float(macd_line.ewm(span=9).mean().iloc[-1])

        bb_mid = float(closes.rolling(20).mean().iloc[-1])
        bb_std = float(closes.rolling(20).std().iloc[-1])

        current = float(closes.iloc[-1])
        avg_vol = float(volumes.rolling(20).mean().iloc[-1])
        vol_ratio = float(volumes.iloc[-1]) / avg_vol if avg_vol > 0 else 1.0

        return {
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2) if ma50 else None,
            "ma200": round(ma200, 2) if ma200 else None,
            "rsi": round(rsi, 2),
            "macd": round(macd, 4),
            "macd_signal": round(macd_signal, 4),
            "bb_upper": round(bb_mid + 2 * bb_std, 2),
            "bb_mid": round(bb_mid, 2),
            "bb_lower": round(bb_mid - 2 * bb_std, 2),
            "volume_ratio": round(vol_ratio, 2),
            "above_ma20": current > ma20,
            "above_ma50": current > ma50 if ma50 else None,
        }
    except Exception as e:
        logger.error(f"[AShare] Error calculating indicators for {symbol}: {e}")
        return {}


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def get_ashare_news(symbol: str, limit: int = 5) -> list:
    """
    Fetch recent news for an A-share from Sina Finance RSS.
    Output format matches market_data.get_stock_news().
    """
    try:
        code = normalize_code(symbol)
        url = (
            f"https://finance.sina.com.cn/realstock/company/{_sina_symbol(code)}/nc.shtml"
        )
        # Sina stock news page — parse headlines via RSS proxy
        # Use the simpler company news RSS endpoint
        rss_url = f"https://rss.sina.com.cn/finance/stock/stock{code}.xml"
        import feedparser
        feed = feedparser.parse(rss_url)
        result = []
        for entry in feed.entries[:limit]:
            result.append({
                "title": entry.get("title", ""),
                "publisher": "新浪财经",
                "link": entry.get("link", ""),
                "published": 0,
            })
        if result:
            return result
    except Exception:
        pass

    # Fallback: return empty list (news not critical for A-share analysis)
    return []


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------

def is_china_market_open() -> bool:
    """
    Return True if Shanghai/Shenzhen market is currently open.
    Sessions (UTC+8 / CST): 09:30-11:30 and 13:00-15:00, Mon-Fri.
    In UTC: 01:30-03:30 and 05:00-07:00.
    """
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5:
        return False
    total = now_utc.hour * 60 + now_utc.minute
    morning = 1 * 60 + 30 <= total <= 3 * 60 + 30
    afternoon = 5 * 60 <= total <= 7 * 60
    return morning or afternoon
