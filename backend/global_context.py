"""
Global Market Context Engine
=============================
Builds a comprehensive real-time snapshot of ALL major global markets before
any individual stock analysis.  The result is injected into every AI prompt so
DeepSeek-R1 can make decisions that account for:

  • Global risk environment (RISK_ON / NEUTRAL / RISK_OFF)
  • US markets: S&P 500, NASDAQ, VIX, 10Y yield, DXY
  • Chinese markets: SSE/SZSE indices, northbound capital flow (北向资金),
    CNY/USD, PBOC liquidity signals
  • Asia: Nikkei, Hang Seng, KOSPI, ASX, Nifty, TWII
  • Europe: DAX, FTSE, CAC, STOXX50
  • Commodities: Gold, Silver, Oil (WTI), Copper
  • Currency matrix: DXY, CNY, JPY, EUR, GBP
  • Sector rotation: XLK, XLE, XLF, XLV, XLI, XLRE
  • Confidence modifiers keyed to risk environment

Usage (in auto-trade loop):
    ctx = build_global_context()   # once per cycle (5-min cache)
    signal = ai.analyze_stock(..., global_context=ctx)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Cache (shared across all users in the same cycle) ─────────────────────────
_CACHE: Dict = {}
_CACHE_TTL = 300   # 5 minutes


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["value"]
    return None


def _cache_set(key: str, value):
    _CACHE[key] = {"value": value, "ts": time.time()}


# ── yfinance batch fetch ───────────────────────────────────────────────────────
def _yf_batch(symbols: List[str], period: str = "2d") -> Dict[str, dict]:
    """Fetch latest close + 1-day change for a list of symbols via yfinance."""
    result = {}
    try:
        import yfinance as yf
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                hist = tickers.tickers[sym].history(period=period)
                if hist.empty:
                    continue
                curr = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else curr
                chg_pct = ((curr - prev) / prev * 100) if prev else 0
                result[sym] = {
                    "price": round(curr, 4),
                    "change_pct": round(chg_pct, 3),
                    "direction": "UP" if chg_pct > 0.1 else ("DOWN" if chg_pct < -0.1 else "FLAT"),
                }
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[GlobalCtx] yfinance batch fetch failed: {e}")
    return result


# ── A-share northbound capital (北向资金) via East Money ───────────────────────
def _get_northbound_flow() -> dict:
    """
    Return latest northbound capital flow (外资净买入A股).
    Positive = foreign money buying A-shares (bullish).
    Negative = foreign money selling (bearish).
    Data source: East Money real-time API.
    """
    try:
        url = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get"
        params = {
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f54,f56",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "cb": "",
        }
        resp = requests.get(url, params=params, timeout=8,
                            headers={"Referer": "https://data.eastmoney.com/",
                                     "User-Agent": "Mozilla/5.0"})
        data = resp.json().get("data", {})
        # s2n = Shanghai-HK connect northbound, s2s = Shenzhen-HK connect northbound
        s2n_list = data.get("s2n", [])
        s2s_list = data.get("s2s", [])

        def _latest_net(lst: list) -> float:
            if not lst:
                return 0.0
            # Each entry: "09:30,xxx,yyy,zzz,www" — last field is cumulative net
            for row in reversed(lst):
                parts = str(row).split(",")
                if len(parts) >= 4 and parts[-1] not in ("", "-"):
                    try:
                        return float(parts[-1])
                    except ValueError:
                        pass
            return 0.0

        sh_net = _latest_net(s2n_list)   # 亿元
        sz_net = _latest_net(s2s_list)
        total_net = round(sh_net + sz_net, 2)
        direction = "INFLOW" if total_net > 0 else ("OUTFLOW" if total_net < 0 else "NEUTRAL")
        return {
            "total_net_bn_cny": total_net,
            "shanghai_net": round(sh_net, 2),
            "shenzhen_net": round(sz_net, 2),
            "direction": direction,
            "signal": "BULLISH" if total_net > 5 else ("BEARISH" if total_net < -5 else "NEUTRAL"),
        }
    except Exception as e:
        logger.debug(f"[GlobalCtx] Northbound flow fetch failed: {e}")
        return {"total_net_bn_cny": 0, "direction": "unknown", "signal": "NEUTRAL"}


# ── CNY/USD rate ──────────────────────────────────────────────────────────────
def _get_cny_rate() -> dict:
    """Get current CNY/USD exchange rate from Sina Finance."""
    try:
        resp = requests.get(
            "https://hq.sinajs.cn/list=fx_susdcny",
            headers={"Referer": "https://finance.sina.com.cn/",
                     "User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        import re
        m = re.search(r'"([^"]+)"', resp.text)
        if m:
            parts = m.group(1).split(",")
            if len(parts) >= 4:
                rate = float(parts[3])  # close price
                prev = float(parts[2])
                chg_pct = (rate - prev) / prev * 100 if prev else 0
                return {
                    "usdcny": round(rate, 4),
                    "change_pct": round(chg_pct, 4),
                    "cny_strong": chg_pct < 0,  # USDCNY falling = CNY getting stronger
                }
    except Exception:
        pass
    # fallback via yfinance
    try:
        import yfinance as yf
        h = yf.Ticker("USDCNY=X").history(period="2d")
        if not h.empty:
            curr = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2]) if len(h) > 1 else curr
            chg = (curr - prev) / prev * 100 if prev else 0
            return {"usdcny": round(curr, 4), "change_pct": round(chg, 4), "cny_strong": chg < 0}
    except Exception:
        pass
    return {"usdcny": 7.2, "change_pct": 0, "cny_strong": False}


# ── Core snapshot builder ─────────────────────────────────────────────────────

def build_global_context(force_refresh: bool = False) -> dict:
    """
    Build a comprehensive global market context snapshot.
    Cached for 5 minutes so the whole auto-trade cycle shares one snapshot.
    """
    cached = _cache_get("global_ctx")
    if cached and not force_refresh:
        return cached

    ctx = _build_global_context_internal()
    _cache_set("global_ctx", ctx)
    return ctx


def _build_global_context_internal() -> dict:
    """Actual data collection – called at most once per TTL period."""
    ts = datetime.now(timezone.utc).isoformat()
    logger.info("[GlobalCtx] Refreshing global market context snapshot...")

    # ── 1. Fetch all key tickers in one batch ─────────────────────────────────
    TICKERS = {
        # US
        "^GSPC": "SP500", "^IXIC": "NASDAQ", "^DJI": "DOW", "^RUT": "RUSSELL2000",
        "^VIX": "VIX",
        # US Bonds & Dollar
        "^TNX": "US_10Y_YIELD", "DX-Y.NYB": "DXY",
        # US Sectors
        "XLK": "SECTOR_TECH", "XLE": "SECTOR_ENERGY", "XLF": "SECTOR_FINANCE",
        "XLV": "SECTOR_HEALTH", "XLI": "SECTOR_INDUS", "XLRE": "SECTOR_REALESTATE",
        "XLU": "SECTOR_UTIL", "XLP": "SECTOR_STAPLES", "XLY": "SECTOR_CONSUMER",
        # Commodities
        "GLD": "GOLD", "SLV": "SILVER", "USO": "OIL_ETF",
        "HG=F": "COPPER",
        # Asia Pacific
        "^N225": "NIKKEI", "^HSI": "HANGSENG", "000001.SS": "SSE_COMPOSITE",
        "^AXJO": "ASX200", "^KS11": "KOSPI", "^TWII": "TAIWAN_WEIGHTED",
        "^NSEI": "NIFTY50",
        # Europe
        "^GDAXI": "DAX", "^FTSE": "FTSE100", "^FCHI": "CAC40", "^STOXX50E": "EUROSTOXX50",
        # Currencies (vs USD)
        "EURUSD=X": "EURUSD", "JPY=X": "USDJPY", "GBPUSD=X": "GBPUSD",
        # Crypto proxy
        "BTC-USD": "BITCOIN",
    }
    sym_list = list(TICKERS.keys())
    raw = _yf_batch(sym_list, period="2d")
    prices: Dict[str, dict] = {}
    for sym, label in TICKERS.items():
        d = raw.get(sym, {})
        prices[label] = d

    # ── 2. Northbound capital & CNY ───────────────────────────────────────────
    northbound = _get_northbound_flow()
    cny = _get_cny_rate()

    # ── 3. Risk Environment Scoring ───────────────────────────────────────────
    score, factors = _compute_risk_score(prices, northbound, cny)
    risk_env = "RISK_ON" if score > 0.2 else ("RISK_OFF" if score < -0.2 else "NEUTRAL")

    # ── 4. Market breadth ─────────────────────────────────────────────────────
    breadth = _compute_breadth(prices)

    # ── 5. VIX level interpretation ───────────────────────────────────────────
    vix_val = prices.get("VIX", {}).get("price", 0) or 0
    vix_level = ("EXTREME_FEAR" if vix_val > 35 else
                 "HIGH_FEAR" if vix_val > 25 else
                 "ELEVATED" if vix_val > 20 else
                 "NORMAL" if vix_val > 14 else "CALM")

    # ── 6. Sector rotation analysis ───────────────────────────────────────────
    sector_rotation = _analyze_sector_rotation(prices)

    # ── 7. Cross-market signals ───────────────────────────────────────────────
    cross_signals = _build_cross_market_signals(prices, cny, northbound)

    # ── 8. Confidence modifiers per market ────────────────────────────────────
    conf_modifiers = _compute_confidence_modifiers(prices, vix_val, risk_env, cny, northbound)

    # ── 9. AI narrative (injected into every prompt) ──────────────────────────
    narrative = _build_ai_narrative(
        prices, risk_env, score, vix_val, vix_level,
        northbound, cny, breadth, sector_rotation, cross_signals
    )

    result = {
        "timestamp": ts,
        "risk_environment": risk_env,
        "risk_score": round(score, 3),       # -1.0 (extreme fear) → +1.0 (extreme greed)
        "risk_factors": factors,
        "vix": {"value": round(vix_val, 2), "level": vix_level},
        "us_markets": {
            "sp500": prices.get("SP500", {}),
            "nasdaq": prices.get("NASDAQ", {}),
            "dow": prices.get("DOW", {}),
            "russell2000": prices.get("RUSSELL2000", {}),
            "us_10y_yield": prices.get("US_10Y_YIELD", {}),
            "dxy": prices.get("DXY", {}),
        },
        "china_markets": {
            "sse_composite": prices.get("SSE_COMPOSITE", {}),
            "northbound_flow": northbound,
            "cny_usd": cny,
        },
        "asia_markets": {
            "nikkei": prices.get("NIKKEI", {}),
            "hangseng": prices.get("HANGSENG", {}),
            "kospi": prices.get("KOSPI", {}),
            "asx200": prices.get("ASX200", {}),
            "nifty50": prices.get("NIFTY50", {}),
        },
        "europe_markets": {
            "dax": prices.get("DAX", {}),
            "ftse100": prices.get("FTSE100", {}),
            "cac40": prices.get("CAC40", {}),
        },
        "commodities": {
            "gold": prices.get("GOLD", {}),
            "silver": prices.get("SILVER", {}),
            "oil": prices.get("OIL_ETF", {}),
            "copper": prices.get("COPPER", {}),
        },
        "currencies": {
            "dxy": prices.get("DXY", {}),
            "eurusd": prices.get("EURUSD", {}),
            "usdjpy": prices.get("USDJPY", {}),
            "usdcny": cny,
        },
        "sector_rotation": sector_rotation,
        "market_breadth": breadth,
        "cross_market_signals": cross_signals,
        "confidence_modifiers": conf_modifiers,
        "ai_narrative": narrative,
    }
    logger.info(f"[GlobalCtx] Built: risk={risk_env}({score:+.2f}) VIX={vix_val:.1f} "
                f"SP500={prices.get('SP500',{}).get('change_pct',0):+.2f}% "
                f"SSE={prices.get('SSE_COMPOSITE',{}).get('change_pct',0):+.2f}% "
                f"NBI={northbound.get('total_net_bn_cny',0):+.1f}亿")
    return result


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _compute_risk_score(prices: dict, northbound: dict, cny: dict) -> Tuple[float, List[str]]:
    """
    Compute a risk appetite score in [-1, +1].
    +1 = maximum risk-on (buy everything), -1 = maximum risk-off (hide in bunkers).
    """
    score = 0.0
    factors = []

    def _chg(label: str) -> float:
        return prices.get(label, {}).get("change_pct", 0) or 0

    # VIX (inverted – high VIX = risk-off)
    vix = prices.get("VIX", {}).get("price", 18) or 18
    if vix > 30:
        score -= 0.3; factors.append(f"VIX极度恐慌 {vix:.1f}")
    elif vix > 22:
        score -= 0.15; factors.append(f"VIX恐惧 {vix:.1f}")
    elif vix < 15:
        score += 0.1; factors.append(f"VIX平静 {vix:.1f}")

    # S&P 500 day change
    sp = _chg("SP500")
    if sp > 1.0:
        score += 0.15; factors.append(f"美股大涨 {sp:+.2f}%")
    elif sp < -1.0:
        score -= 0.15; factors.append(f"美股大跌 {sp:+.2f}%")

    # NASDAQ
    nq = _chg("NASDAQ")
    if nq > 1.5:
        score += 0.1; factors.append(f"纳指强势 {nq:+.2f}%")
    elif nq < -1.5:
        score -= 0.1; factors.append(f"纳指弱势 {nq:+.2f}%")

    # DXY (strong dollar = EM headwind, risk-off)
    dxy = _chg("DXY")
    if dxy > 0.5:
        score -= 0.1; factors.append(f"美元走强 DXY {dxy:+.2f}%")
    elif dxy < -0.5:
        score += 0.08; factors.append(f"美元走弱 DXY {dxy:+.2f}%")

    # Gold (gold rising = risk-off signal, but can be inflation hedge)
    gold = _chg("GOLD")
    if gold > 1.0 and sp < 0:
        score -= 0.08; factors.append(f"黄金避险上涨 {gold:+.2f}%")

    # Copper (copper falling = growth slowdown risk)
    copper = _chg("COPPER")
    if copper < -1.5:
        score -= 0.08; factors.append(f"铜价下跌-经济走弱 {copper:+.2f}%")
    elif copper > 1.5:
        score += 0.06; factors.append(f"铜价上涨-经济向好 {copper:+.2f}%")

    # US 10Y yield (rising yield = headwind for growth stocks)
    ty = _chg("US_10Y_YIELD")
    if ty > 3:   # yield up >3bps
        score -= 0.05; factors.append(f"美债收益率上升 {ty:+.2f}%")

    # China northbound capital
    nbi = northbound.get("total_net_bn_cny", 0)
    if nbi > 10:
        score += 0.07; factors.append(f"北向资金大幅流入 +{nbi:.1f}亿")
    elif nbi < -10:
        score -= 0.07; factors.append(f"北向资金大幅流出 {nbi:.1f}亿")

    # CNY strengthening
    if cny.get("cny_strong") and abs(cny.get("change_pct", 0)) > 0.2:
        score += 0.04; factors.append(f"人民币走强 USDCNY {cny.get('usdcny')}")
    elif not cny.get("cny_strong") and abs(cny.get("change_pct", 0)) > 0.3:
        score -= 0.05; factors.append(f"人民币走弱 USDCNY {cny.get('usdcny')}")

    # Hang Seng (CN offshore market sentiment)
    hsi = _chg("HANGSENG")
    if hsi > 1.5:
        score += 0.06; factors.append(f"港股大涨 {hsi:+.2f}%")
    elif hsi < -1.5:
        score -= 0.06; factors.append(f"港股大跌 {hsi:+.2f}%")

    # Bitcoin (crypto = risk-on canary)
    btc = _chg("BITCOIN")
    if btc > 3:
        score += 0.04; factors.append(f"比特币大涨 {btc:+.2f}%")
    elif btc < -5:
        score -= 0.05; factors.append(f"比特币大跌 {btc:+.2f}%")

    return max(-1.0, min(1.0, score)), factors


def _compute_breadth(prices: dict) -> dict:
    """How many major global indices are up vs down today?"""
    indices = [
        "SP500", "NASDAQ", "DOW", "NIKKEI", "HANGSENG", "SSE_COMPOSITE",
        "ASX200", "KOSPI", "DAX", "FTSE100", "CAC40", "EUROSTOXX50",
        "NIFTY50", "TAIWAN_WEIGHTED",
    ]
    up = sum(1 for k in indices if (prices.get(k, {}).get("change_pct", 0) or 0) > 0.1)
    down = sum(1 for k in indices if (prices.get(k, {}).get("change_pct", 0) or 0) < -0.1)
    total = up + down
    breadth_pct = (up / total * 100) if total > 0 else 50
    return {
        "up": up, "down": down, "flat": len(indices) - up - down,
        "breadth_pct": round(breadth_pct, 1),
        "label": ("强劲上涨" if breadth_pct > 75 else
                  "普遍上涨" if breadth_pct > 55 else
                  "分化震荡" if breadth_pct > 45 else
                  "普遍下跌" if breadth_pct > 25 else "全面下跌"),
    }


def _analyze_sector_rotation(prices: dict) -> dict:
    """Identify which sectors are leading vs lagging."""
    sectors = {
        "科技 (XLK)": prices.get("SECTOR_TECH", {}).get("change_pct", 0) or 0,
        "能源 (XLE)": prices.get("SECTOR_ENERGY", {}).get("change_pct", 0) or 0,
        "金融 (XLF)": prices.get("SECTOR_FINANCE", {}).get("change_pct", 0) or 0,
        "医疗 (XLV)": prices.get("SECTOR_HEALTH", {}).get("change_pct", 0) or 0,
        "工业 (XLI)": prices.get("SECTOR_INDUS", {}).get("change_pct", 0) or 0,
        "地产 (XLRE)": prices.get("SECTOR_REALESTATE", {}).get("change_pct", 0) or 0,
        "消费 (XLY)": prices.get("SECTOR_CONSUMER", {}).get("change_pct", 0) or 0,
        "公用 (XLU)": prices.get("SECTOR_UTIL", {}).get("change_pct", 0) or 0,
    }
    sorted_sectors = sorted(sectors.items(), key=lambda x: x[1], reverse=True)
    leaders = [s[0] for s in sorted_sectors[:3] if s[1] > 0.1]
    laggards = [s[0] for s in sorted_sectors[-3:] if s[1] < -0.1]

    # Determine rotation theme
    tech_chg = sectors.get("科技 (XLK)", 0)
    energy_chg = sectors.get("能源 (XLE)", 0)
    util_chg = sectors.get("公用 (XLU)", 0)
    fin_chg = sectors.get("金融 (XLF)", 0)

    theme = "均衡"
    if tech_chg > 1.0 and tech_chg > energy_chg + 0.5:
        theme = "科技成长主导"
    elif energy_chg > 1.0 and energy_chg > tech_chg + 0.5:
        theme = "能源/商品轮动"
    elif util_chg > 0.5 and tech_chg < 0:
        theme = "防御性轮动"
    elif fin_chg > 1.0:
        theme = "金融/顺周期轮动"
    elif util_chg > 0.3 and energy_chg > 0.3 and tech_chg < 0:
        theme = "避险/防御"

    return {
        "theme": theme,
        "leaders": leaders,
        "laggards": laggards,
        "sectors": {k: round(v, 3) for k, v in sectors.items()},
    }


def _build_cross_market_signals(prices: dict, cny: dict, northbound: dict) -> List[str]:
    """Identify important cross-market relationships and flags."""
    signals = []

    sp = prices.get("SP500", {}).get("change_pct", 0) or 0
    nq = prices.get("NASDAQ", {}).get("change_pct", 0) or 0
    sse = prices.get("SSE_COMPOSITE", {}).get("change_pct", 0) or 0
    hsi = prices.get("HANGSENG", {}).get("change_pct", 0) or 0
    nk = prices.get("NIKKEI", {}).get("change_pct", 0) or 0
    jpy_chg = prices.get("USDJPY", {}).get("change_pct", 0) or 0
    gold = prices.get("GOLD", {}).get("change_pct", 0) or 0
    oil = prices.get("OIL_ETF", {}).get("change_pct", 0) or 0
    dxy = prices.get("DXY", {}).get("change_pct", 0) or 0
    vix = prices.get("VIX", {}).get("price", 18) or 18
    copper = prices.get("COPPER", {}).get("change_pct", 0) or 0

    # A股 vs 港股 divergence (A-H premium signal)
    if sse > 1.0 and hsi < -0.5:
        signals.append("A股强/港股弱：A-H溢价扩大，A股可能存在相对高估风险")
    elif hsi > 1.0 and sse < -0.5:
        signals.append("港股强/A股弱：外资偏好离岸中国资产，关注港股机会")
    elif sse > 0.5 and hsi > 0.5:
        signals.append("A股港股同涨：中国资产整体向好，内外资共振")

    # Japan yen carry trade signal
    if jpy_chg > 0.5 and nk < -1.0:
        signals.append(f"日元走强+日经下跌：套息交易反转信号，可能引发全球风险资产抛售")
    elif jpy_chg < -0.5 and nk > 0.5:
        signals.append(f"日元走弱+日经上涨：出口商受益，套息交易继续，风险偏好改善")

    # Gold + Dollar divergence
    if gold > 0.5 and dxy > 0.3:
        signals.append("黄金+美元双涨：极度避险情绪，地缘政治或系统性风险事件")
    elif gold > 1.0 and sp < -0.5:
        signals.append("黄金避险买盘强烈，股市下跌：资金逃向安全资产")

    # Oil signals
    if oil > 2.0:
        signals.append(f"原油大涨 {oil:+.2f}%：关注能源股(XOM/CVX/BP)，航空/运输股承压")
    elif oil < -2.0:
        signals.append(f"原油大跌 {oil:+.2f}%：通胀预期降温，能源股承压，消费/航空获益")

    # Copper (Dr. Copper economic signal)
    if copper > 1.5:
        signals.append(f"铜价上涨 {copper:+.2f}%：全球经济复苏信号，周期股/工业股受益")
    elif copper < -1.5:
        signals.append(f"铜价大跌 {copper:+.2f}%：经济放缓担忧，防御性持仓")

    # Tech sector dominance
    tech_chg = prices.get("SECTOR_TECH", {}).get("change_pct", 0) or 0
    if tech_chg < -1.5 and nq < -1.0:
        signals.append("美科技股全线杀跌：NVDA/AAPL/MSFT类持仓面临系统性压力")
    elif tech_chg > 1.5:
        signals.append("美科技板块强势领涨：AI相关股票短期动能良好")

    # North bound capital signal
    nbi = northbound.get("total_net_bn_cny", 0) or 0
    if nbi > 20:
        signals.append(f"北向资金大幅净流入 +{nbi:.1f}亿：外资加仓A股，看好中国市场")
    elif nbi < -20:
        signals.append(f"北向资金大幅净流出 {nbi:.1f}亿：外资撤离A股，短期谨慎")

    # VIX spike
    if vix > 30:
        signals.append(f"⚠️ VIX={vix:.1f} 极度恐慌：市场处于高波动期，建议降低仓位或等待企稳")

    # Global breadth signal
    if sp > 0.5 and nk > 0.5 and hsi > 0.5 and sse > 0:
        signals.append("全球主要市场同步上涨：全球风险偏好回升，趋势交易有利")
    elif sp < -0.5 and nk < -0.5 and hsi < -0.5:
        signals.append("全球主要市场同步下跌：系统性卖压，非单一股票问题")

    return signals[:8]  # cap at 8 signals


def _compute_confidence_modifiers(prices: dict, vix_val: float,
                                   risk_env: str, cny: dict, northbound: dict) -> dict:
    """
    Returns a dict of multipliers that should be applied to AI signal confidence.
    Keys: market codes (US, CN, HK, JP, EU, EM, GOLD_SLV)
    Values: float multiplier (0.5 = halve confidence, 1.2 = boost 20%)
    """
    mods = {
        "US": 1.0, "CN": 1.0, "HK": 1.0, "JP": 1.0,
        "EU": 1.0, "EM": 1.0, "GOLD_SLV": 1.0, "ALL": 1.0,
    }
    sp_chg = prices.get("SP500", {}).get("change_pct", 0) or 0

    # VIX modifiers
    if vix_val > 35:
        mods["ALL"] *= 0.6       # extreme fear – severely cut BUY confidence
    elif vix_val > 25:
        mods["ALL"] *= 0.8       # elevated fear
    elif vix_val < 14:
        mods["ALL"] *= 1.1       # complacency – slight boost but not too much

    # Bear market filter (S&P below MA20 is checked elsewhere, but day-level)
    if sp_chg < -2.0:
        mods["US"] *= 0.7
        mods["ALL"] *= 0.85

    # Gold/Silver boost during risk-off
    if risk_env == "RISK_OFF":
        mods["GOLD_SLV"] *= 1.2
        mods["US"] *= 0.85
        mods["EM"] *= 0.7

    # DXY impact on EM
    dxy_chg = prices.get("DXY", {}).get("change_pct", 0) or 0
    if dxy_chg > 0.8:
        mods["EM"] *= 0.75       # strong dollar hurts EM
        mods["HK"] *= 0.85

    # CNY impact on China stocks
    cny_chg = cny.get("change_pct", 0) or 0
    if cny_chg > 0.4:           # CNY weakening (USDCNY rising)
        mods["CN"] *= 0.8
    elif cny_chg < -0.3:        # CNY strengthening
        mods["CN"] *= 1.1

    # Northbound capital impact on CN BUY confidence
    nbi = northbound.get("total_net_bn_cny", 0) or 0
    if nbi > 15:
        mods["CN"] *= 1.15
    elif nbi < -15:
        mods["CN"] *= 0.75

    # HK + China combo
    hsi_chg = prices.get("HANGSENG", {}).get("change_pct", 0) or 0
    if hsi_chg < -2.0:
        mods["HK"] *= 0.7

    # JPY carry unwind
    jpy_chg = prices.get("USDJPY", {}).get("change_pct", 0) or 0
    if jpy_chg > 0.8:           # yen strengthening = carry unwind = sell risk
        mods["JP"] *= 0.8
        mods["ALL"] *= 0.9

    # Risk-on boosts
    if risk_env == "RISK_ON":
        mods["US"] *= 1.08
        mods["CN"] = min(mods["CN"] * 1.05, 1.3)

    # Clamp all values
    for k in mods:
        mods[k] = round(max(0.3, min(1.5, mods[k])), 3)

    return mods


# ── AI narrative builder ──────────────────────────────────────────────────────

def _build_ai_narrative(prices, risk_env, score, vix_val, vix_level,
                         northbound, cny, breadth, sector_rotation, cross_signals) -> str:
    """
    Build a concise but comprehensive narrative string for injection into AI prompts.
    Written in Chinese for better reasoning by Chinese-trained models.
    """
    def _fmt(label: str, key: str, prefix: str = "", suffix: str = "%") -> str:
        chg = prices.get(key, {}).get("change_pct", None)
        if chg is None:
            return f"{label}: N/A"
        sign = "+" if chg >= 0 else ""
        return f"{label}: {prefix}{sign}{chg:.2f}{suffix}"

    def _fmt_price(label: str, key: str, prefix: str = "") -> str:
        p = prices.get(key, {}).get("price", None)
        if p is None:
            return f"{label}: N/A"
        return f"{label}: {prefix}{p:.2f}"

    risk_icon = "🟢" if risk_env == "RISK_ON" else ("🔴" if risk_env == "RISK_OFF" else "🟡")
    lines = [
        f"### 🌍 全球市场动态 (实时综合分析)",
        f"**风险环境: {risk_icon} {risk_env}** (综合评分: {score:+.3f}, 范围-1.0到+1.0)",
        f"**市场广度**: {breadth['label']} ({breadth['up']}涨/{breadth['down']}跌/{breadth['flat']}平)",
        "",
        "**📊 美国市场**",
        f"  {_fmt('标普500', 'SP500')}  |  {_fmt('纳斯达克', 'NASDAQ')}  |  {_fmt_price('VIX', 'VIX')} ({vix_level})",
        f"  {_fmt('美元指数DXY', 'DXY')}  |  {_fmt('10Y美债收益率', 'US_10Y_YIELD')}",
        "",
        "**🇨🇳 中国市场**",
        f"  {_fmt('上证综指', 'SSE_COMPOSITE')}  |  {_fmt('港股恒生', 'HANGSENG')}",
        f"  北向资金: {northbound.get('total_net_bn_cny', 0):+.1f}亿元 ({northbound.get('direction','N/A')}, {northbound.get('signal','N/A')})",
        f"  USDCNY: {cny.get('usdcny', 7.2):.4f} (变化 {cny.get('change_pct', 0):+.4f}%)",
        "",
        "**🌏 亚太市场**",
        f"  {_fmt('日经225', 'NIKKEI')}  |  {_fmt('韩国KOSPI', 'KOSPI')}  |  {_fmt('澳洲ASX', 'ASX200')}  |  {_fmt('印度NIFTY', 'NIFTY50')}",
        "",
        "**🇪🇺 欧洲市场**",
        f"  {_fmt('德DAX', 'DAX')}  |  {_fmt('英FTSE', 'FTSE100')}  |  {_fmt('法CAC', 'CAC40')}",
        "",
        "**💎 大宗商品**",
        f"  {_fmt('黄金(GLD)', 'GOLD')}  |  {_fmt('白银(SLV)', 'SILVER')}  |  {_fmt('原油', 'OIL_ETF')}  |  {_fmt('铜', 'COPPER')}",
        "",
        f"**🔄 板块轮动**: {sector_rotation['theme']}",
    ]
    if sector_rotation.get("leaders"):
        lines.append(f"  领涨板块: {', '.join(sector_rotation['leaders'])}")
    if sector_rotation.get("laggards"):
        lines.append(f"  落后板块: {', '.join(sector_rotation['laggards'])}")

    if cross_signals:
        lines.append("\n**⚡ 跨市场信号 & 联动分析**")
        for s in cross_signals:
            lines.append(f"  • {s}")

    # Decision guidance
    lines.append("\n**🎯 基于全球动态的决策指引**")
    if risk_env == "RISK_ON":
        lines.append("  ✅ 全球风险偏好积极。支持适度进攻性持仓，可考虑成长股和周期股机会。")
    elif risk_env == "RISK_OFF":
        lines.append("  ⚠️ 全球风险规避模式。提高信号门槛，优先防御性资产(GLD/SLV/债券ETF)，减少成长股暴露。")
    else:
        lines.append("  🟡 全球市场中性分化。以个股基本面为主要决策依据，谨慎对待方向性下注。")

    if vix_val > 25:
        lines.append(f"  🚨 VIX={vix_val:.1f} 高度恐惧：所有BUY信号置信度应降低，等待企稳信号。")

    return "\n".join(lines)


# ── Helpers for per-symbol modifier application ───────────────────────────────

def get_confidence_modifier(global_ctx: dict, symbol: str) -> float:
    """
    Return the confidence multiplier for a specific symbol given global context.
    Used by auto-trade loop to adjust AI confidence before executing trades.
    """
    if not global_ctx:
        return 1.0
    from market_calendar import detect_market
    market = detect_market(symbol)
    mods = global_ctx.get("confidence_modifiers", {})
    base = mods.get("ALL", 1.0)
    specific = mods.get(market, 1.0)
    # Special case for gold/silver ETFs
    sym_upper = symbol.upper()
    if sym_upper in ("GLD", "SLV", "IAU", "GDX", "GDXJ"):
        specific = mods.get("GOLD_SLV", 1.0)
    combined = round(base * specific, 3)
    return max(0.3, min(1.5, combined))


def get_global_context_summary(global_ctx: dict) -> str:
    """Return a short 1-line summary of global context for logging."""
    if not global_ctx:
        return "No global context"
    risk = global_ctx.get("risk_environment", "UNKNOWN")
    score = global_ctx.get("risk_score", 0)
    vix = global_ctx.get("vix", {}).get("value", 0)
    return f"GlobalCtx: {risk}({score:+.2f}) VIX={vix:.1f}"
