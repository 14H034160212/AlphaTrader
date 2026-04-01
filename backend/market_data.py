"""Market data service using yfinance + Sina Finance for global stock market data."""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging
from quant_models import QuantitativeModels
import ashare_data
from market_calendar import detect_market, get_currency

logger = logging.getLogger(__name__)

# ── Global market indices ─────────────────────────────────────────────────────
GLOBAL_INDICES = {
    # ── Americas ──────────────────────────────────────────────────────────────
    "Americas": {
        "^GSPC":   {"name": "S&P 500",      "region": "US"},
        "^IXIC":   {"name": "NASDAQ",        "region": "US"},
        "^DJI":    {"name": "道琼斯",         "region": "US"},
        "^RUT":    {"name": "Russell 2000",  "region": "US"},
        "^VIX":    {"name": "VIX 恐慌指数",  "region": "US"},
        "^BVSP":   {"name": "巴西 Bovespa",  "region": "Brazil"},
        "^MXX":    {"name": "墨西哥 IPC",    "region": "Mexico"},
        "^GSPTSE": {"name": "加拿大 TSX",    "region": "Canada"},
        "^MERV":   {"name": "阿根廷 MERVAL", "region": "Argentina"},
        "^IPSA":   {"name": "智利 IPSA",     "region": "Chile"},
    },
    # ── Europe ────────────────────────────────────────────────────────────────
    "Europe": {
        "^FTSE":    {"name": "英国 FTSE 100",    "region": "UK"},
        "^GDAXI":   {"name": "德国 DAX",          "region": "Germany"},
        "^FCHI":    {"name": "法国 CAC 40",        "region": "France"},
        "^STOXX50E":{"name": "欧洲 Stoxx 50",     "region": "EU"},
        "^AEX":     {"name": "荷兰 AEX",          "region": "Netherlands"},
        "FTSEMIB.MI":{"name": "意大利 FTSE MIB",  "region": "Italy"},
        "^IBEX":    {"name": "西班牙 IBEX 35",    "region": "Spain"},
        "^SSMI":    {"name": "瑞士 SMI",          "region": "Switzerland"},
        "^OMX":     {"name": "斯德哥尔摩 OMX",   "region": "Sweden"},
        "IMOEX.ME": {"name": "俄罗斯 MOEX",       "region": "Russia"},
        "^BFX":     {"name": "比利时 BEL 20",     "region": "Belgium"},
    },
    # ── Asia Pacific ──────────────────────────────────────────────────────────
    "Asia Pacific": {
        "^N225":   {"name": "日经 225",         "region": "Japan"},
        "^HSI":    {"name": "恒生指数",          "region": "HongKong"},
        "^AXJO":   {"name": "澳大利亚 ASX 200", "region": "Australia"},
        "^STI":    {"name": "新加坡 STI",       "region": "Singapore"},
        "^KS11":   {"name": "韩国 KOSPI",       "region": "SouthKorea"},
        "^TWII":   {"name": "台湾加权指数",      "region": "Taiwan"},
        "^NSEI":   {"name": "印度 NIFTY 50",    "region": "India"},
        "^BSESN":  {"name": "印度 SENSEX",      "region": "India"},
        "^KLSE":   {"name": "马来西亚 KLCI",    "region": "Malaysia"},
        "^SET.BK": {"name": "泰国 SET",         "region": "Thailand"},
        "^JKSE":   {"name": "印尼 IDX",         "region": "Indonesia"},
        "^PSI":    {"name": "菲律宾 PSEi",      "region": "Philippines"},
    },
    # ── China A-shares ────────────────────────────────────────────────────────
    "China A": {
        "000001.SS": {"name": "上证指数",   "region": "China"},
        "399001.SZ": {"name": "深证成指",   "region": "China"},
        "399006.SZ": {"name": "创业板指",   "region": "China"},
        "000300.SS": {"name": "沪深300",    "region": "China"},
        "000016.SS": {"name": "上证50",     "region": "China"},
        "399905.SZ": {"name": "中证500",    "region": "China"},
        "000688.SS": {"name": "科创50",     "region": "China"},
        "^HSI":      {"name": "恒生指数",   "region": "HongKong"},
        "^HSCE":     {"name": "国企指数",   "region": "HongKong"},
    },
    # ── Middle East & Africa ──────────────────────────────────────────────────
    "Middle East & Africa": {
        "^TASI.SR": {"name": "沙特 Tadawul", "region": "Saudi Arabia"},
        "^DFMGI":   {"name": "迪拜 DFM",     "region": "UAE"},
        "^TA125.TA":{"name": "以色列 TA-125","region": "Israel"},
        "^J203.JO": {"name": "南非 JSE Top40","region": "South Africa"},
        "^EGX30":   {"name": "埃及 EGX 30",  "region": "Egypt"},
    },
}

# ── Popular international stocks (yfinance tickers) ───────────────────────────
# These are organized by region for easy discovery

GLOBAL_POPULAR_STOCKS = {
    "US_TECH": [
        "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA",
        "AMD", "AVGO", "TSM", "ASML", "QCOM", "MU", "INTC",
    ],
    # Enterprise software / cloud — large caps that often move on restructuring news
    "US_ENTERPRISE": ["ORCL", "CRM", "NOW", "SAP", "ADBE", "IBM", "INTU", "WDAY",
                      "CSCO", "ACN", "CTSH", "FISV", "CDNS", "SNPS"],
    # US-listed ADRs / dual-listings of major global tech companies (Alpaca-tradeable)
    # These are the global tech giants where layoff/restructuring news = buy signal
    "GLOBAL_TECH_ADR": [
        "BABA",   # 阿里巴巴 Alibaba (NYSE)
        "BIDU",   # 百度 Baidu (NASDAQ)
        "JD",     # 京东 JD.com (NASDAQ)
        "PDD",    # 拼多多 Pinduoduo/Temu (NASDAQ)
        "TCEHY",  # 腾讯 Tencent (OTC)
        "NTES",   # 网易 NetEase (NASDAQ)
        "BEKE",   # 贝壳 KE Holdings (NYSE)
        "INFY",   # Infosys (NYSE ADR)
        "WIT",    # Wipro (NYSE ADR)
        "ERIC",   # Ericsson (NASDAQ)
        "NOK",    # Nokia (NYSE)
        "SONY",   # Sony (NYSE ADR)
        "NTDOY",  # Nintendo (OTC ADR)
        "SFTBY",  # SoftBank (OTC ADR)
        "TSM",    # 台积电 TSMC (NYSE ADR) — also in US_TECH
        "ASML",   # ASML (NASDAQ) — also in US_TECH
        "SAP",    # SAP (NYSE ADR) — also in US_ENTERPRISE
        "SHOP",   # Shopify (NYSE) — Canadian tech
        "SE",     # Sea Limited (NYSE) — Southeast Asia tech
        "GRAB",   # Grab (NASDAQ) — Southeast Asia
    ],
    "US_FINANCE": ["JPM", "BAC", "GS", "MS", "BLK", "V", "MA", "AXP"],
    "US_ENERGY":  ["XOM", "CVX", "COP", "OXY", "SLB"],
    "US_ETF":     ["SPY", "QQQ", "TQQQ", "SOXL", "GLD", "SLV", "IAU", "IWM",
                   "IBIT", "MSTR", "COIN"],
    # US-accessible global ETFs — tradeable via Alpaca, no Futu/IBKR needed
    "GLOBAL_ETF": [
        "EWJ",   # Japan (iShares MSCI Japan)
        "FXI",   # China Large-Cap (iShares China)
        "EWT",   # Taiwan (iShares MSCI Taiwan)
        "EWY",   # South Korea (iShares MSCI South Korea)
        "EWG",   # Germany (iShares MSCI Germany)
        "EWU",   # UK (iShares MSCI UK)
        "EWA",   # Australia (iShares MSCI Australia)
        "EWZ",   # Brazil (iShares MSCI Brazil)
        "INDA",  # India (iShares MSCI India)
        "VGK",   # Europe (Vanguard FTSE Europe)
        "EEM",   # Emerging Markets (iShares MSCI EM)
        "EFA",   # Developed Markets ex-US (iShares MSCI EAFE)
    ],
    "HK": [
        "0700.HK",   # 腾讯 Tencent
        "9988.HK",   # 阿里巴巴 Alibaba
        "3690.HK",   # 美团 Meituan
        "9618.HK",   # 京东 JD.com
        "1810.HK",   # 小米 Xiaomi
        "0941.HK",   # 中国移动 China Mobile
        "2318.HK",   # 中国平安 Ping An
        "1299.HK",   # 友邦保险 AIA
        "0005.HK",   # 汇丰银行 HSBC
        "0388.HK",   # 港交所 HKEX
        "2269.HK",   # 药明生物 WuXi Bio
        "1211.HK",   # 比亚迪 BYD
        "0175.HK",   # 吉利汽车 Geely
        "6690.HK",   # 海尔智家 Haier
        "9999.HK",   # 网易 NetEase
    ],
    "CN_ASHARE": [
        "600519.SH",  # 贵州茅台
        "000858.SZ",  # 五粮液
        "600036.SH",  # 招商银行
        "601318.SH",  # 中国平安
        "600276.SH",  # 恒瑞医药
        "300750.SZ",  # 宁德时代
        "601888.SH",  # 中国中免
        "000333.SZ",  # 美的集团
        "002594.SZ",  # 比亚迪
        "601628.SH",  # 中国人寿
        "600900.SH",  # 长江电力
        "601166.SH",  # 兴业银行
        "000001.SZ",  # 平安银行
        "600031.SH",  # 三一重工
        "688981.SH",  # 中芯国际 (STAR)
    ],
    "JP": [
        "7203.T",   # Toyota
        "6758.T",   # Sony
        "6861.T",   # Keyence
        "9984.T",   # SoftBank
        "8306.T",   # MUFG
        "6502.T",   # Toshiba
        "7267.T",   # Honda
        "9433.T",   # KDDI
    ],
    "EU": [
        "SAP.DE",    # SAP (Germany)
        "ASML.AS",   # ASML (Netherlands) - US ADR also: ASML
        "MC.PA",     # LVMH (France)
        "OR.PA",     # L'Oréal (France)
        "NESN.SW",   # Nestlé (Switzerland)
        "ROG.SW",    # Roche (Switzerland)
        "NOVN.SW",   # Novartis (Switzerland)
        "SHEL.L",    # Shell (UK)
        "AZN.L",     # AstraZeneca (UK)
        "BP.L",      # BP (UK)
        "HSBA.L",    # HSBC (UK)
        "SIE.DE",    # Siemens (Germany)
        "BAYN.DE",   # Bayer (Germany)
        "BMW.DE",    # BMW (Germany)
        "VOW3.DE",   # Volkswagen (Germany)
    ],
    "AU": [
        "BHP.AX",    # BHP Group
        "CBA.AX",    # Commonwealth Bank
        "CSL.AX",    # CSL Limited
        "NAB.AX",    # NAB
        "RIO.AX",    # Rio Tinto
        "WBC.AX",    # Westpac
    ],
    "KR": [
        "005930.KS",  # Samsung Electronics
        "000660.KS",  # SK Hynix
        "005380.KS",  # Hyundai Motor
        "035420.KS",  # NAVER
        "051910.KS",  # LG Chem
    ],
    "IN": [
        "RELIANCE.NS",  # Reliance Industries
        "TCS.NS",       # Tata Consultancy
        "INFY.NS",      # Infosys
        "HDFCBANK.NS",  # HDFC Bank
        "ICICIBANK.NS", # ICICI Bank
    ],
    "BR": [
        "VALE3.SA",   # Vale
        "PETR4.SA",   # Petrobras
        "ITUB4.SA",   # Itaú Unibanco
        "WEGE3.SA",   # WEG
        "ABEV3.SA",   # Ambev
    ],
    "SG": [
        "D05.SI",  # DBS Group
        "U11.SI",  # UOB
        "O39.SI",  # OCBC
        "Z74.SI",  # Singtel
        "C09.SI",  # City Dev
    ],
}

# Default watchlist (US-focused, backward compatible)
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
    "IBIT", "MSTR", "COIN",
    # ---- Gold & Silver ----
    "GLD", "SLV",
    # ---- HK / China ADR ----
    "0700.HK", "9988.HK", "1810.HK",
    # ---- Defense ----
    "LMT", "RTX",
]


def get_index_data(symbol: str) -> dict:
    """Fetch current data for a market index."""
    # CN A-share indices → Sina Finance
    if ashare_data.is_ashare_symbol(symbol):
        data = ashare_data.get_ashare_quote(symbol)
        if data:
            return {
                "symbol": symbol,
                "current": data["current"],
                "change": data["change"],
                "change_pct": data["change_pct"],
                "volume": data["volume"],
                "timestamp": data["timestamp"],
            }
        return None
    try:
        ticker = yf.Ticker(symbol)
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
            "currency": get_currency(symbol),
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error fetching index {symbol}: {e}")
        return None


def get_all_indices() -> dict:
    """Fetch all global market indices."""
    from market_calendar import is_market_open, detect_market
    result = {}
    for region, indices in GLOBAL_INDICES.items():
        result[region] = []
        for symbol, meta in indices.items():
            data = get_index_data(symbol)
            if data:
                mkt = detect_market(symbol)
                data.update(meta)
                data["market_open"] = is_market_open(mkt)
                data["market_code"] = mkt
                result[region].append(data)
    return result


def get_global_popular_stocks(region: str = None) -> list:
    """Return list of popular international stock symbols, optionally filtered by region."""
    if region and region in GLOBAL_POPULAR_STOCKS:
        return GLOBAL_POPULAR_STOCKS[region]
    all_stocks = []
    for stocks in GLOBAL_POPULAR_STOCKS.values():
        all_stocks.extend(stocks)
    return list(dict.fromkeys(all_stocks))  # deduplicate while preserving order


def get_stock_quote(symbol: str) -> dict:
    """Fetch current quote for a stock (all markets)."""
    if ashare_data.is_ashare_symbol(symbol):
        return ashare_data.get_ashare_quote(symbol)
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
            "currency": info.get("currency", "") or get_currency(symbol),
            "exchange": info.get("exchange", ""),
            "market": detect_market(symbol),
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
    if ashare_data.is_ashare_symbol(symbol):
        return ashare_data.get_ashare_history(symbol, period, interval)
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
    if ashare_data.is_ashare_symbol(symbol):
        return ashare_data.get_ashare_indicators(symbol)
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty or len(hist) < 20:
            return {}

        closes = hist["Close"]
        highs  = hist["High"]
        lows   = hist["Low"]
        volumes = hist["Volume"]

        # ATR (Average True Range, 14-day) — used for adaptive stop-loss
        prev_close = closes.shift(1)
        tr = (highs - lows).combine(
            (highs - prev_close).abs(), max
        ).combine(
            (lows - prev_close).abs(), max
        )
        atr14 = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else float(highs.iloc[-1] - lows.iloc[-1])

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

        # RSI State
        rsi_state = "NEUTRAL"
        if rsi > 70: rsi_state = "OVERBOUGHT"
        elif rsi < 30: rsi_state = "OVERSOLD"
        
        # MA200 Distance
        dist_ma200 = ((current - ma200) / ma200) if ma200 else 0

        return {
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2) if ma50 else None,
            "ma200": round(ma200, 2) if ma200 else None,
            "dist_from_ma200_pct": round(dist_ma200 * 100, 2) if ma200 else 0,
            "rsi": round(rsi, 2),
            "rsi_state": rsi_state,
            "macd": round(macd, 4),
            "macd_signal": round(signal, 4),
            "bb_upper": round(bb_upper, 2),
            "bb_mid": round(bb_mid, 2),
            "bb_lower": round(bb_lower, 2),
            "volume_ratio": round(volume_ratio, 2),
            "above_ma20": current > ma20,
            "above_ma50": current > ma50 if ma50 else None,
            "above_ma200": current > ma200 if ma200 else None,
            "atr14": round(atr14, 4),
            "atr14_pct": round(atr14 / current * 100, 2) if current > 0 else 0,
        }
    except Exception as e:
        logger.error(f"Error calculating indicators for {symbol}: {e}")
        return {}


def get_stock_news(symbol: str, limit: int = 5) -> list:
    """Fetch recent news for a stock."""
    if ashare_data.is_ashare_symbol(symbol):
        return ashare_data.get_ashare_news(symbol, limit)
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
    """Search for stocks by symbol or name (global markets)."""
    results = []
    q = query.strip()

    # Try as-is first (handles AAPL, 0700.HK, 600519.SH, SAP.DE, etc.)
    for sym in [q, q.upper()]:
        try:
            if ashare_data.is_ashare_symbol(sym):
                quote = ashare_data.get_ashare_quote(sym)
                if quote:
                    results.append({
                        "symbol": sym,
                        "name": quote.get("name", sym),
                        "exchange": quote.get("exchange", ""),
                        "sector": quote.get("sector", "A股"),
                        "currency": "CNY",
                        "market": "CN",
                    })
                    return results
            else:
                ticker = yf.Ticker(sym)
                info = ticker.info
                if info.get("longName") or info.get("shortName"):
                    results.append({
                        "symbol": sym.upper(),
                        "name": info.get("longName") or info.get("shortName", sym),
                        "exchange": info.get("exchange", ""),
                        "sector": info.get("sector", ""),
                        "currency": info.get("currency", get_currency(sym)),
                        "market": detect_market(sym),
                    })
                    return results
        except Exception:
            pass

    # Search popular international stocks by name keyword
    search_lower = q.lower()
    for stocks in GLOBAL_POPULAR_STOCKS.values():
        for sym in stocks:
            if search_lower in sym.lower():
                results.append({"symbol": sym, "name": sym, "exchange": "", "sector": "",
                                 "currency": get_currency(sym), "market": detect_market(sym)})

    return results[:10]


def get_all_market_statuses() -> dict:
    """Return open/closed status for all global markets."""
    from market_calendar import get_all_market_statuses as _get_statuses
    return _get_statuses()


def get_multi_market_quotes(symbols: list) -> dict:
    """
    Batch-fetch quotes for a list of symbols from multiple markets.
    Returns {symbol: quote_dict} mapping.
    """
    result = {}
    for symbol in symbols:
        try:
            quote = get_stock_quote(symbol)
            if quote:
                result[symbol] = quote
        except Exception as e:
            logger.debug(f"[MultiMarket] Could not fetch {symbol}: {e}")
    return result
