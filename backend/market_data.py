"""Market data service using yfinance for global stock market data."""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging
from quant_models import QuantitativeModels

logger = logging.getLogger(__name__)

# Global market indices
GLOBAL_INDICES = {
    "Americas": {
        "^GSPC": {"name": "S&P 500", "region": "US"},
        "^IXIC": {"name": "NASDAQ", "region": "US"},
        "^DJI": {"name": "Dow Jones", "region": "US"},
        "^RUT": {"name": "Russell 2000", "region": "US"},
        "^BVSP": {"name": "Bovespa", "region": "Brazil"},
        "^MXX": {"name": "IPC Mexico", "region": "Mexico"},
    },
    "Europe": {
        "^FTSE": {"name": "FTSE 100", "region": "UK"},
        "^GDAXI": {"name": "DAX", "region": "Germany"},
        "^FCHI": {"name": "CAC 40", "region": "France"},
        "^STOXX50E": {"name": "Euro Stoxx 50", "region": "EU"},
        "^AEX": {"name": "AEX", "region": "Netherlands"},
        "FTSEMIB.MI": {"name": "FTSE MIB", "region": "Italy"},
    },
    "Asia Pacific": {
        "^N225": {"name": "Nikkei 225", "region": "Japan"},
        "^HSI": {"name": "Hang Seng", "region": "HongKong"},
        "000001.SS": {"name": "Shanghai Composite", "region": "China"},
        "^AXJO": {"name": "ASX 200", "region": "Australia"},
        "^STI": {"name": "STI", "region": "Singapore"},
        "^KS11": {"name": "KOSPI", "region": "SouthKorea"},
    }
}

# Popular stocks to track
DEFAULT_WATCHLIST = [
    # ---- AI & Semiconductors ----
    "NVDA", "TSM", "ASML", "AMD", "AVGO",
    # ---- Big Tech & E-Commerce ----
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    # ---- Value & Financials ----
    "JPM", "V", "XOM", "UNH",
    # ---- Leveraged & Broad Market ETFs ----
    "SPY", "QQQ", "TQQQ", "SOXL",
    # ---- Crypto Proxy ----
    "IBIT", "MSTR", "COIN"
]


def get_index_data(symbol: str) -> dict:
    """Fetch current data for a market index."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        hist = ticker.history(period="2d")
        if hist.empty:
            return None
        current = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else current
        change = current - prev
        change_pct = (change / prev * 100) if prev != 0 else 0
        return {
            "symbol": symbol,
            "current": round(current, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 3),
            "volume": int(hist["Volume"].iloc[-1]) if not pd.isna(hist["Volume"].iloc[-1]) else 0,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error fetching index {symbol}: {e}")
        return None


def get_all_indices() -> dict:
    """Fetch all global market indices."""
    result = {}
    for region, indices in GLOBAL_INDICES.items():
        result[region] = []
        for symbol, meta in indices.items():
            data = get_index_data(symbol)
            if data:
                data.update(meta)
                result[region].append(data)
    return result


def get_stock_quote(symbol: str) -> dict:
    """Fetch current quote for a stock."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        hist = ticker.history(period="20d")
        if hist.empty:
            return None

        current = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else current
        change = current - prev
        change_pct = (change / prev * 100) if prev != 0 else 0

        # Extract Fundamentals for Models
        fcf = info.get("freeCashflow", 0)
        total_debt = info.get("totalDebt", 0)
        cash = info.get("totalCash", 0)
        shares_out = info.get("sharesOutstanding", 0)
        div_rate = info.get("dividendRate", 0.0)

        # Calculate Intrinsic Values
        dcf_value = QuantitativeModels.calculate_dcf(fcf, total_debt, cash, shares_out)
        ddm_value = QuantitativeModels.calculate_ddm(div_rate) if div_rate else 0.0
        
        # Use DDM for high dividend stocks, else DCF (Whichever is higher provides a safer floor, or just average)
        intrinsic_value = ddm_value if div_rate > 0 and ddm_value > dcf_value else dcf_value
        valuation_gap = QuantitativeModels.calculate_valuation_gap(current, intrinsic_value)

        # Calculate Microstructure (VPA)
        hist_records = []
        for idx, row in hist.iterrows():
            hist_records.append({
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"])
            })
        vpa_metrics = QuantitativeModels.analyze_volume_price_action(hist_records)

        return {
            "symbol": symbol,
            "name": info.get("longName", symbol),
            "current": round(current, 2),
            "open": round(float(hist["Open"].iloc[-1]), 2),
            "high": round(float(hist["High"].iloc[-1]), 2),
            "low": round(float(hist["Low"].iloc[-1]), 2),
            "volume": int(hist["Volume"].iloc[-1]),
            "change": round(change, 2),
            "change_pct": round(change_pct, 3),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "sector": info.get("sector", ""),
            "currency": info.get("currency", "USD"),
            "exchange": info.get("exchange", ""),
            "timestamp": datetime.utcnow().isoformat(),
            "dcf_value": round(dcf_value, 2),
            "ddm_value": round(ddm_value, 2),
            "intrinsic_value": round(intrinsic_value, 2),
            "valuation_gap_pct": round(valuation_gap, 3),
            "vpa_signal": vpa_metrics["vpa_signal"],
            "crowding": vpa_metrics["crowding"],
            "liquidity": vpa_metrics["liquidity"],
            "vpa_volume_ratio": vpa_metrics["volume_ratio"],
        }
    except Exception as e:
        logger.error(f"Error fetching stock {symbol}: {e}")
        return None


def get_stock_history(symbol: str, period: str = "3mo", interval: str = "1d") -> list:
    """Fetch OHLCV historical data for charting."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty:
            return []
        result = []
        for idx, row in hist.iterrows():
            result.append({
                "time": int(idx.timestamp()),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            })
        return result
    except Exception as e:
        logger.error(f"Error fetching history for {symbol}: {e}")
        return []


def get_technical_indicators(symbol: str) -> dict:
    """Calculate basic technical indicators."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty or len(hist) < 20:
            return {}

        closes = hist["Close"]
        volumes = hist["Volume"]

        # Moving averages
        ma20 = float(closes.rolling(20).mean().iloc[-1])
        ma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
        ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None

        # RSI
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = float(100 - (100 / (1 + rs)).iloc[-1])

        # MACD
        ema12 = closes.ewm(span=12).mean()
        ema26 = closes.ewm(span=26).mean()
        macd = float((ema12 - ema26).iloc[-1])
        signal = float((ema12 - ema26).ewm(span=9).mean().iloc[-1])

        # Bollinger Bands
        bb_mid = float(closes.rolling(20).mean().iloc[-1])
        bb_std = float(closes.rolling(20).std().iloc[-1])
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std

        current = float(closes.iloc[-1])
        avg_volume = float(volumes.rolling(20).mean().iloc[-1])
        volume_ratio = float(volumes.iloc[-1]) / avg_volume if avg_volume > 0 else 1

        return {
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2) if ma50 else None,
            "ma200": round(ma200, 2) if ma200 else None,
            "rsi": round(rsi, 2),
            "macd": round(macd, 4),
            "macd_signal": round(signal, 4),
            "bb_upper": round(bb_upper, 2),
            "bb_mid": round(bb_mid, 2),
            "bb_lower": round(bb_lower, 2),
            "volume_ratio": round(volume_ratio, 2),
            "above_ma20": current > ma20,
            "above_ma50": current > ma50 if ma50 else None,
        }
    except Exception as e:
        logger.error(f"Error calculating indicators for {symbol}: {e}")
        return {}


def get_stock_news(symbol: str, limit: int = 5) -> list:
    """Fetch recent news for a stock."""
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        result = []
        for item in news[:limit]:
            result.append({
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "link": item.get("link", ""),
                "published": item.get("providerPublishTime", 0),
            })
        return result
    except Exception as e:
        logger.error(f"Error fetching news for {symbol}: {e}")
        return []


def search_stocks(query: str) -> list:
    """Search for stocks by symbol or name."""
    try:
        results = []
        # Try direct symbol lookup
        ticker = yf.Ticker(query.upper())
        info = ticker.info
        if info.get("longName"):
            results.append({
                "symbol": query.upper(),
                "name": info.get("longName", ""),
                "exchange": info.get("exchange", ""),
                "sector": info.get("sector", ""),
            })
        return results
    except Exception as e:
        logger.error(f"Error searching for {query}: {e}")
        return []
