"""
Dynamic watchlist discovery.

Replaces the static hand-curated watchlist with market-driven auto-discovery.
Eliminates the keyword-maintenance anti-pattern (user feedback 2026-05-24:
"watchlist 不应该写死，而是根据市场信息来动态调整的").

Discovery sources
=================
1. **Trending / top movers** — yfinance get_screener + Yahoo trending tickers.
   Picks up momentum names like SNDK (40x rally) that we'd never have
   hand-added.
2. **News mention frequency** — count ticker mentions in recent RSS news
   across major financial outlets; high-frequency tickers get added.
3. **Social sentiment surge** — names with extreme StockTwits sentiment
   (|score| ≥ 0.5) and high message volume.
4. **Peer expansion** — for each current holding/watchlist symbol, find
   sector peers via static SECTOR_PEERS and add the top ones.
5. **Existing held positions** — anything we currently hold (Alpaca / Futu /
   IBKR) must stay in watchlist (already-bought = needs monitoring).

Pruning
=======
- Stocks with no AI signals in last 14 days AND no current position →
  candidate for removal.
- BUT: hard cap watchlist at MAX_WATCHLIST_SIZE (default 150). When over cap,
  prune oldest stale names first.
- Never prune CORE_ETF or anything user explicitly pinned.

Output: persists to Settings.watchlist as JSON list, atomic update.
"""
from __future__ import annotations
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── Discovery thresholds ────────────────────────────────────────────────────
MAX_WATCHLIST_SIZE         = 200        # raised 5/26 from 150 to give room for
                                         # thematic discoveries (Physical AI + GPU
                                         # downstream at-bottom names). LLM cost
                                         # still bounded — cache hit rate dominates
                                         # after first warm cycle.
TRENDING_MIN_MARKET_CAP_B  = 1.0        # $1B min so we don't add penny stocks
TRENDING_MIN_ABS_CHANGE_PCT = 5.0       # daily |pct| ≥ 5% qualifies as "trending"
NEWS_MIN_MENTIONS          = 3          # ≥3 distinct news headlines mentioning ticker = relevant
SENTIMENT_MIN_SCORE        = 0.5        # |StockTwits sentiment| ≥ 0.5 = surge
PRUNE_NO_ACTIVITY_DAYS     = 14         # no signals or positions for N days → eligible to drop


# Always-keep core (user-protected — never pruned even if stale)
# Categories user explicitly cares about must be here.
ALWAYS_KEEP = {
    # Index / ETF anchors
    "SPY", "QQQ", "VOO", "IVV", "VTI",
    "GLD", "SLV", "IBIT", "TLT",
    # ── Memory / Storage (user directive 2026-05-24: 内存股请一定要考虑) ──
    "MU", "SNDK", "WDC", "STX", "NTAP",
    # ── China exposure via US-listed ETFs (user 2026-05-24 confirmed Moomoo AU
    # does NOT support CN A-shares native — use ETF proxies instead) ──
    "FXI",    # iShares China Large-Cap
    "MCHI",   # iShares MSCI China
    "ASHR",   # Xtrackers Harvest CSI 300 China A (direct A-share 300)
    "KWEB",   # KraneShares China Internet
    "KBA",    # KraneShares Bosera MSCI China A
    # ── Core US Semi (the AI cycle backbone) ──
    "NVDA", "AMD", "TSM", "AVGO", "ASML", "ARM", "QCOM", "MRVL",
    "LRCX", "KLAC", "AMAT", "INTC", "SMCI",
    # ── Core HK / China-listed Semi (user directive 2026-05-28: track even
    # though 1 board lot is currently unaffordable on HK$10k — auto-trigger
    # once user deposits more HKD). 1-lot cost: 中芯~42.6k, 华虹~152k,
    # ASM~20k, 复旦微~37k, 舜宇~7.5k (the only one affordable now). ──
    "0981.HK",   # SMIC 中芯国际 — China's largest foundry
    "1347.HK",   # Hua Hong Semi 华虹半导体 — foundry
    "0522.HK",   # ASM Pacific 先进封装设备
    "1385.HK",   # Shanghai Fudan Micro 复旦微
    "2382.HK",   # Sunny Optical 舜宇光学 — optics/sensors (affordable now)
    # ── AI infrastructure peripherals (power, networking, cooling) ──
    "VRT",    # Vertiv (data center cooling)
    "ETN",    # Eaton (electrification)
    "PWR",    # Quanta Services (grid build)
    "CEG",    # Constellation Energy (nuclear AI)
    "VST",    # Vistra Energy
    "ANET",   # Arista (data center networking)
    "DELL",   # Dell AI servers
    "HPE",    # HPE AI servers
    # ── Mega-cap tech ──
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA",
}


# Sector peer map — when one peer in watchlist, auto-add others.
SECTOR_PEERS = {
    "NVDA": ["AMD","INTC","TSM","AVGO","MU","ARM","QCOM","MRVL","LRCX","KLAC","AMAT","ASML","SMCI"],
    "MU":   ["SNDK","WDC","STX","NTAP","MCHP","ON","TXN"],     # memory/storage
    "SNDK": ["MU","WDC","STX","NTAP"],
    "ANET": ["DELL","HPE","JNPR","CSCO","ANET"],               # data center networking
    "VRT":  ["ETN","PWR","CEG","VST","NEE","D"],               # AI power/cooling
    "AAPL": ["MSFT","GOOGL","META","AMZN","NFLX"],
    "JPM":  ["BAC","WFC","C","GS","MS","V","MA"],
    "TSLA": ["RIVN","LCID","NIO","XPEV","BYD","LI","F","GM"],
    "BABA": ["JD","PDD","BIDU","NTES","TCOM","BILI","9988.HK","0700.HK","9618.HK"],
}


# Newly-added grace period: a freshly added symbol gets this many days before
# the stale-signal prune logic touches it. Without this, anything added 2h ago
# would be pruned the next cycle (no signals yet).
NEW_SYMBOL_GRACE_DAYS = 7


# ── Thematic universes (user 2026-05-26 directive: Physical AI + Robotics
# + GPU downstream supply chain). These are SEED pools, not the watchlist.
# discover_thematic_at_bottom() picks names from these that are currently
# trading near 52w lows ("at bottom") per the user filter rule.
THEMATIC_UNIVERSES = {
    "core_semiconductors": [
        # GPU / AI accelerators
        "NVDA", "AMD", "AVGO", "TSM", "ARM",
        # CPU / logic
        "INTC", "QCOM", "MRVL",
        # Memory / storage
        "MU", "SNDK", "WDC", "STX",
        # Equipment / EDA
        "ASML", "LRCX", "KLAC", "AMAT", "SNPS", "CDNS",
        # Analog / power / other
        "TXN", "ADI", "NXPI", "ON", "MCHP", "MPWR",
        # AI server / systems
        "SMCI", "DELL", "ANET",
        # Foundry / packaging
        "GFS", "UMC", "ASE", "AMKR",
    ],
    "physical_ai_robotics": [
        # Surgical / medical
        "ISRG",
        # Industrial automation
        "ROK", "EMR", "HON", "ROP", "IR", "PNR", "FELE",
        # Robotics components / motion
        "AMBA", "MTSI", "FANUY", "ABBNY",
        # Autonomous / humanoid (US-listed)
        "TSLA",       # Optimus
        "PATH",       # UiPath RPA
        "NVDA",       # robotics SoC
        "SYM",        # warehouse robotics
        "RBOT",       # surgical robotics
        # Robotics-focused ETFs
        "BOTZ", "ROBO", "ROBT", "IRBO", "ARKQ", "URTY",
        # HK robotics / sensor names
        "02382.HK",   # Sunny Optical
        "09866.HK",   # Robosense
    ],
    "gpu_downstream_supply": [
        # ── HBM / memory / storage ──
        "MU", "SNDK", "WDC", "STX",
        # ── Wafer fab equipment (WFE) ──
        "ASML", "LRCX", "KLAC", "AMAT", "AEHR", "ONTO", "VECO", "ICHR", "KLIC", "UCTT", "ACLS",
        # ── EDA / IP ──
        "SNPS", "CDNS", "ARM",
        # ── Power semi / power delivery for AI servers ──
        "ON", "MPWR", "VICR", "POWI", "ALAB", "NVT", "ENS",
        # ── Optical / networking / interconnect ──
        "ANET", "COHR", "CIEN", "LITE", "FN", "AAOI", "CRDO", "MRVL", "SMCI",
        # ── Cooling / thermal / data-center physical ──
        "VRT", "FLEX", "CLS", "JBL", "MOD", "BE",
        # ── Packaging / advanced packaging / substrate ──
        "ASE", "AMKR", "WFG", "ENTG", "MTRN", "CCMP",
        # ── Test / inspection / probe ──
        "CAMT", "FORM", "TER", "COHU",
        # ── Foundry / IDM ──
        "TSM", "GFS", "UMC", "INTC",
        # ── AI server / systems / OEM (chip consumers) ──
        "DELL", "HPE", "SMCI", "NTAP", "PSTG", "CRM",
        # ── AI power generation (data center electricity demand) ──
        "VST", "CEG", "TLN", "NRG", "ETN", "PWR", "GEV",
        # ── Hyperscaler / cloud (largest chip buyers) ──
        "MSFT", "GOOGL", "AMZN", "META", "ORCL",
        # ── AI software / applications running on the chips ──
        "PLTR", "NOW", "SNOW", "AI",
        # ── 港股芯片/半导体链 (HK-listed China semis — same chip-focus thesis) ──
        "0981.HK",   # SMIC 中芯国际 (China's largest foundry)
        "1347.HK",   # Hua Hong Semi 华虹半导体 (foundry)
        "0522.HK",   # ASM Pacific 先进封装设备
        "1385.HK",   # Shanghai Fudan Microelectronics
        "2382.HK",   # Sunny Optical 舜宇光学 (optics/sensors)
        "0285.HK",   # BYD Electronic (components)
        "1810.HK",   # Xiaomi (chips/hardware)
        "0700.HK",   # Tencent (AI/cloud — chip buyer)
        "9988.HK",   # Alibaba HK (AI/cloud — chip buyer)
        "9618.HK",   # JD HK (cloud)
        "3750.HK",   # CATL HK (battery — AI power adjacent)
    ],
}


def discover_thematic_at_bottom(
    max_pct_above_52w_low: float = 0.40,    # within 40% of 52w low
    max_pct_below_52w_high: float = 0.70,   # i.e. at most 70% of 52w high
    min_market_cap_b: float = 1.0,
) -> list[tuple[str, str, float]]:
    """
    For each thematic universe, find symbols currently 'at bottom':
      current_price ≤ (1 + max_pct_above_52w_low) × 52w_low
      AND current_price ≤ max_pct_below_52w_high × 52w_high

    Returns [(symbol, theme_name, drawdown_pct_from_high), ...]
    """
    import yfinance as yf
    results = []
    seen = set()
    for theme, syms in THEMATIC_UNIVERSES.items():
        for sym in syms:
            if sym in seen:
                continue
            seen.add(sym)
            try:
                # Skip non-yfinance tickers (HK Moomoo handles those)
                if sym.endswith(".HK"):
                    continue
                t = yf.Ticker(sym)
                info = t.info
                last = info.get("currentPrice") or info.get("regularMarketPrice")
                lo52 = info.get("fiftyTwoWeekLow")
                hi52 = info.get("fiftyTwoWeekHigh")
                mc = (info.get("marketCap") or 0) / 1e9
                if not (last and lo52 and hi52 and mc >= min_market_cap_b):
                    continue
                if lo52 <= 0 or hi52 <= 0:
                    continue
                pct_above_low = (last - lo52) / lo52
                pct_below_high = last / hi52
                if (pct_above_low <= max_pct_above_52w_low
                        and pct_below_high <= max_pct_below_52w_high):
                    drawdown = (last - hi52) / hi52 * 100
                    results.append((sym, theme, drawdown))
            except Exception as e:
                logger.debug(f"[Thematic] {sym} skipped: {e}")
    return results


def _get_db_session():
    from database import SessionLocal
    return SessionLocal()


def _load_current_watchlist(db) -> list[str]:
    from database import Settings
    r = db.query(Settings).filter(Settings.key=="watchlist").first()
    if not r or not r.value:
        return []
    try:
        return json.loads(r.value)
    except json.JSONDecodeError:
        return []


def _persist_watchlist(db, syms: list[str]) -> None:
    from database import Settings
    r = db.query(Settings).filter(Settings.key=="watchlist").first()
    deduped = list(dict.fromkeys(syms))  # preserve order, drop dupes
    if r:
        r.value = json.dumps(deduped)
    else:
        db.add(Settings(user_id=1, key="watchlist", value=json.dumps(deduped)))
    db.commit()


# ── Discovery sources ───────────────────────────────────────────────────────


def discover_trending_us() -> list[tuple[str, float, float]]:
    """
    Pull US top movers via yfinance screener.
    Returns list of (symbol, change_pct, market_cap_billion) tuples.
    """
    import yfinance as yf
    results = []
    try:
        for screener_name in ("day_gainers", "day_losers", "most_actives"):
            try:
                snap = yf.screen(screener_name, count=50)
                for r in (snap.get("quotes", []) or []):
                    sym = r.get("symbol", "")
                    if not sym or "." in sym:
                        continue
                    chg = r.get("regularMarketChangePercent", 0) or 0
                    mc = (r.get("marketCap", 0) or 0) / 1e9
                    if mc >= TRENDING_MIN_MARKET_CAP_B and abs(chg) >= TRENDING_MIN_ABS_CHANGE_PCT:
                        results.append((sym, chg, mc))
            except Exception as e:
                logger.debug(f"[Discover] screener {screener_name} failed: {e}")
    except Exception as e:
        logger.warning(f"[Discover] yfinance trending fetch failed: {e}")
    return results


# HK Hang Seng + Hang Seng Tech universe (curated list of 50 most-traded names)
HK_UNIVERSE = [
    # HK semiconductors (chip focus — added 2026-05-27)
    "HK.00981","HK.01347","HK.00522","HK.01385","HK.00285","HK.03750",
    "HK.00700","HK.09988","HK.03690","HK.01810","HK.09618","HK.01024",
    "HK.01211","HK.09866","HK.02382","HK.02628","HK.00388","HK.00005",
    "HK.00939","HK.01398","HK.03988","HK.00941","HK.00883","HK.00857",
    "HK.00386","HK.01088","HK.02318","HK.02628","HK.01299","HK.00027",
    "HK.00175","HK.02333","HK.01211","HK.00688","HK.01109","HK.01113",
    "HK.00012","HK.00016","HK.01997","HK.00001","HK.00002","HK.00003",
    "HK.00006","HK.00011","HK.06862","HK.06618","HK.02020","HK.02331",
    "HK.06098","HK.03888","HK.06160","HK.00322","HK.09633","HK.06690",
    "HK.02020","HK.06865","HK.09888","HK.01347","HK.06682",
]

# CSI 300 top constituents (curated) — for CN A-share discovery
CN_UNIVERSE = [
    "SH.600519","SH.601318","SZ.000858","SZ.000333","SZ.300750",
    "SH.601012","SH.600036","SH.601398","SH.601288","SH.601988",
    "SH.601166","SZ.000651","SZ.000725","SH.600276","SH.600030",
    "SH.601628","SH.601888","SH.600887","SH.600028","SH.601857",
    "SZ.002594","SH.600009","SH.600196","SZ.002475","SZ.300059",
    "SH.601336","SH.600585","SH.600438","SZ.002714","SH.601066",
    "SH.601800","SH.601319","SH.601229","SZ.000063","SZ.000568",
    "SH.600406","SH.601633","SH.600690","SZ.000661","SH.600009",
]


def discover_trending_via_moomoo(universe: list[str], market_suffix: str,
                                  min_change_pct: float = TRENDING_MIN_ABS_CHANGE_PCT) -> list[tuple[str, float, float]]:
    """
    Pull HK/CN top movers from a curated universe via Moomoo snapshot.

    universe: list of "HK.XXXXX" or "SH.XXXXXX" / "SZ.XXXXXX" codes
    market_suffix: ".HK" / ".SH" / ".SZ" — used to convert back to alphatrader format
    Returns [(symbol_in_at_format, change_pct, market_cap_billion), ...]
    """
    results = []
    try:
        import futu as ft
        ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            # Moomoo snapshot accepts max ~400 codes; chunk if needed
            for i in range(0, len(universe), 200):
                chunk = universe[i:i+200]
                ret, snap = ctx.get_market_snapshot(chunk)
                if ret != ft.RET_OK or snap is None or snap.empty:
                    continue
                for _, row in snap.iterrows():
                    code_futu = row.get("code", "")
                    prev = float(row.get("prev_close_price", 0) or 0)
                    last = float(row.get("last_price", 0) or 0)
                    if prev <= 0 or last <= 0:
                        continue
                    chg_pct = (last - prev) / prev * 100
                    if abs(chg_pct) < min_change_pct:
                        continue
                    # Convert HK.00700 → 0700.HK, SH.600519 → 600519.SH
                    parts = code_futu.split(".")
                    if len(parts) == 2:
                        sym_at = f"{parts[1]}{market_suffix}"
                    else:
                        continue
                    # Market cap from Moomoo (in local currency, convert to USD-ish billions)
                    mc_local = float(row.get("market_val", 0) or 0)
                    mc_b = mc_local / 1e9 / (7.8 if market_suffix == ".HK" else 7.2 if market_suffix in (".SH",".SZ") else 1.0)
                    results.append((sym_at, chg_pct, mc_b))
        finally:
            ctx.close()
    except Exception as e:
        logger.debug(f"[Discover] Moomoo {market_suffix} discovery failed: {e}")
    return results


def discover_trending_hk() -> list[tuple[str, float, float]]:
    """HK top movers from Hang Seng / HS Tech universe."""
    return discover_trending_via_moomoo(HK_UNIVERSE, ".HK")


def discover_trending_cn() -> list[tuple[str, float, float]]:
    """CN A-share top movers from CSI 300 universe."""
    # Split SH vs SZ
    sh_universe = [c for c in CN_UNIVERSE if c.startswith("SH.")]
    sz_universe = [c for c in CN_UNIVERSE if c.startswith("SZ.")]
    results = []
    results.extend(discover_trending_via_moomoo(sh_universe, ".SH"))
    results.extend(discover_trending_via_moomoo(sz_universe, ".SZ"))
    return results


def discover_news_mentions(hours_back: int = 24, min_mentions: int = NEWS_MIN_MENTIONS) -> list[tuple[str, int]]:
    """
    Count ticker mentions across recent financial news (geopolitical + general).
    Returns [(symbol, mention_count), ...] for symbols mentioned ≥ min_mentions.

    Cheap text-search heuristic — looks for ticker symbol uppercase +
    parenthesized form like "(NVDA)" or "Sandisk (SNDK)".
    """
    import re
    import news_intelligence as ni
    counter: Counter = Counter()
    try:
        news = ni.fetch_geopolitical_news(hours_back=hours_back) or []
    except Exception as e:
        logger.debug(f"[Discover] news fetch failed: {e}")
        return []
    # Pattern: $XYZ or (XYZ) — 2-5 uppercase letters
    pat = re.compile(r"\(([A-Z]{2,5})\)|\$([A-Z]{2,5})\b")
    for item in news:
        text = (item.get("title","") or "") + " " + (item.get("summary","") or "")
        for m in pat.finditer(text):
            tkr = m.group(1) or m.group(2)
            counter[tkr] += 1
    # Filter junk (common English uppercase words mistaken for tickers)
    JUNK = {"AI","CEO","CFO","ETF","API","USD","GDP","CPI","FED","UK","EU","US","UN","IT","TV","SEC","FDA","IRS","SUV","DOG"}
    return [(s, c) for s, c in counter.most_common(50)
            if c >= min_mentions and s not in JUNK and len(s) >= 3]


def discover_sentiment_surge(db) -> list[str]:
    """
    Read recent social_sentiment_scan ALERTs from log + DB. Returns symbols
    with strong sentiment in last 12h.
    """
    # Simple: find symbols flagged BULLISH in social_sentiment + already not in
    # the watchlist. Best-effort grep on alphatrader log.
    import subprocess
    try:
        out = subprocess.check_output(
            ["grep", "SocialScan.*ALERT", "/tmp/alphatrader.log"],
            text=True, timeout=5
        )
    except Exception:
        return []
    surged = set()
    import re
    pat = re.compile(r"ALERT: ([A-Z0-9.]{1,12}) is (BULLISH|BEARISH) \(([+\-]?\d+\.?\d*)\)")
    for line in out.splitlines()[-200:]:  # last 200 alerts
        m = pat.search(line)
        if m and abs(float(m.group(3))) >= SENTIMENT_MIN_SCORE:
            surged.add(m.group(1))
    return list(surged)


def discover_peer_expansion(current_wl: list[str]) -> list[str]:
    """Add sector peers for current watchlist symbols."""
    peers = set()
    for sym in current_wl:
        if sym in SECTOR_PEERS:
            peers.update(SECTOR_PEERS[sym])
    return list(peers - set(current_wl))


def get_held_symbols(db) -> set[str]:
    """Anything we currently hold must stay in watchlist."""
    from database import Position, get_setting
    held = set()
    # DB positions
    try:
        for p in db.query(Position).all():
            if p.quantity and p.quantity > 0:
                held.add(p.symbol)
    except Exception:
        pass
    # Alpaca positions (live)
    try:
        import requests
        key = get_setting(db,"alpaca_api_key",1,"")
        sec = get_setting(db,"alpaca_secret_key",1,"")
        if key and sec:
            r = requests.get("https://api.alpaca.markets/v2/positions",
                             headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":sec},
                             timeout=8)
            if r.status_code == 200:
                for p in r.json():
                    if float(p.get("qty",0)) > 0:
                        held.add(p["symbol"])
    except Exception as e:
        logger.debug(f"[Discover] Alpaca positions fetch failed: {e}")
    return held


# ── Pruning ──────────────────────────────────────────────────────────────────


def identify_stale(db, current_wl: list[str], held: set[str]) -> list[str]:
    """Symbols with no AI signal in N days AND no position = stale.

    Grace: a symbol gets NEW_SYMBOL_GRACE_DAYS before its first 'no signal'
    counts against it (otherwise newly-added names get pruned the next cycle).
    """
    from database import AISignal
    cutoff = datetime.utcnow() - timedelta(days=PRUNE_NO_ACTIVITY_DAYS)
    grace_cutoff = datetime.utcnow() - timedelta(days=NEW_SYMBOL_GRACE_DAYS)
    stale = []
    for sym in current_wl:
        if sym in held or sym in ALWAYS_KEEP:
            continue
        # First-signal-ever check — anything whose oldest signal is < grace
        # period is still in onboarding, don't prune.
        first_sig = db.query(AISignal).filter(AISignal.symbol == sym).order_by(AISignal.timestamp.asc()).first()
        if first_sig is None or first_sig.timestamp >= grace_cutoff:
            continue   # still in onboarding grace
        # Stale if no signal in last N days
        n = db.query(AISignal).filter(
            AISignal.symbol == sym,
            AISignal.timestamp >= cutoff,
        ).count()
        if n == 0:
            stale.append(sym)
    return stale


# ── Main orchestration ──────────────────────────────────────────────────────


def run_discovery_cycle(db_session=None) -> dict:
    """
    Full discovery cycle: pull new candidates, prune stale, persist.
    Returns report dict with what changed.
    """
    db = db_session or _get_db_session()
    own_db = db_session is None
    try:
        current = _load_current_watchlist(db)
        held = get_held_symbols(db)

        # ── Collect additions from all sources ──
        adds: set[str] = set()
        sources: dict[str, list] = {}

        trending_us = discover_trending_us()
        sources["trending_us"] = [(s, f"{c:+.1f}%", f"${m:.1f}B") for s, c, m in trending_us]
        adds.update(s for s, _, _ in trending_us)

        # HK dynamic discovery (always-on)
        trending_hk = discover_trending_hk()
        sources["trending_hk"] = [(s, f"{c:+.1f}%", f"HK${m:.1f}B") for s, c, m in trending_hk]
        adds.update(s for s, _, _ in trending_hk)

        # CN A-share discovery — only if Moomoo CN access is enabled.
        # Moomoo AU doesn't sell A-shares to retail (confirmed 2026-05-24);
        # skip the work to avoid wasted snapshot calls. Capability monitor in
        # send_market_reports.sh will detect if CN gets enabled later, at which
        # point this auto-reactivates.
        try:
            import futu as ft
            ctx_cn = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.CN,
                                              host="127.0.0.1", port=11111,
                                              security_firm=ft.SecurityFirm.FUTUAU)
            ret, accs = ctx_cn.get_acc_list()
            ctx_cn.close()
            cn_enabled = ret == ft.RET_OK and len(accs) > 0
        except Exception:
            cn_enabled = False
        if cn_enabled:
            trending_cn = discover_trending_cn()
            sources["trending_cn"] = [(s, f"{c:+.1f}%", f"¥{m:.1f}B") for s, c, m in trending_cn]
            adds.update(s for s, _, _ in trending_cn)
        else:
            sources["trending_cn"] = "(skipped — Moomoo CN not enabled)"

        mentions = discover_news_mentions()
        sources["news"] = mentions[:20]
        adds.update(s for s, _ in mentions)

        sentiment = discover_sentiment_surge(db)
        sources["sentiment"] = sentiment
        adds.update(sentiment)

        peers = discover_peer_expansion(current)
        sources["peers"] = peers[:20]
        adds.update(peers)

        # Thematic discovery (user 2026-05-26: physical AI + GPU downstream
        # at bottom). Only picks names currently within 40% of 52w low.
        # NOTE: stored separately so they rank HIGH in prioritization (user
        # explicitly cares about these themes).
        thematic = discover_thematic_at_bottom()
        sources["thematic_at_bottom"] = [(s, theme, f"{dd:.1f}% off high") for s, theme, dd in thematic]
        thematic_syms = {s for s, _, _ in thematic}
        adds.update(thematic_syms)

        # Pre-existing watchlist + held positions stay; ALWAYS_KEEP also forces
        # add (not just protect) so memory/AI-infra always in scope.
        adds.update(ALWAYS_KEEP - set(current))
        keep = set(current) | held | ALWAYS_KEEP
        # Stale removal candidates (pruned only if we'd exceed cap)
        stale = identify_stale(db, current, held)

        # Build new list with priority:
        #   1. held positions (must monitor)
        #   2. ALWAYS_KEEP (user-pinned categories: memory, AI infra, indexes)
        #   3. thematic_at_bottom (user explicit theme directive 5/26)
        #   4. existing non-stale
        #   5. fresh general discoveries (trending / news / sentiment / peers)
        # Truncate at MAX_WATCHLIST_SIZE — earlier in this list = higher priority.
        always_keep_list = [s for s in ALWAYS_KEEP if s not in held]
        thematic_new = [s for s in thematic_syms
                        if s not in held and s not in ALWAYS_KEEP]
        non_stale_existing = [s for s in current
                              if (s not in stale or s in held or s in ALWAYS_KEEP)
                              and s not in held and s not in ALWAYS_KEEP
                              and s not in thematic_syms]
        general_new = [s for s in (adds - keep - thematic_syms)
                       if s not in held and s not in ALWAYS_KEEP]
        prioritized = list(dict.fromkeys(
            list(held)
            + always_keep_list
            + thematic_new
            + non_stale_existing
            + general_new
        ))

        # Truncate to MAX_WATCHLIST_SIZE
        if len(prioritized) > MAX_WATCHLIST_SIZE:
            # Drop tail (stale & lowest-priority new adds last)
            prioritized = prioritized[:MAX_WATCHLIST_SIZE]

        # Actually-pruned = was in current, not in prioritized, not held/keep
        pruned = [s for s in current if s not in prioritized]
        added_now = [s for s in prioritized if s not in current]

        if added_now or pruned:
            _persist_watchlist(db, prioritized)

        report = {
            "ran_at": datetime.utcnow().isoformat(),
            "before_count": len(current),
            "after_count": len(prioritized),
            "added": sorted(added_now)[:20],
            "added_count": len(added_now),
            "pruned": sorted(pruned)[:20],
            "pruned_count": len(pruned),
            "held_count": len(held),
            "sources": {k: len(v) for k, v in sources.items()},
            "sources_detail": sources,
        }
        logger.info(
            f"[DynamicWL] cycle: {report['before_count']} → {report['after_count']} symbols  "
            f"(+{report['added_count']} -{report['pruned_count']})  "
            f"sources: {report['sources']}"
        )
        if added_now:
            logger.info(f"[DynamicWL] added: {added_now[:15]}")
        if pruned:
            logger.info(f"[DynamicWL] pruned: {pruned[:15]}")
        return report
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    r = run_discovery_cycle()
    print(json.dumps(r, indent=2, default=str)[:3000])
