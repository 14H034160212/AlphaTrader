"""
News Intelligence - Competitive Disruption Detection
Monitors news for second-order impacts: when Company A announces something
that threatens Company B's core business, even if Company B has no news itself.

Classic example: Anthropic announces Claude Code COBOL automation
→ IBM's consulting revenue threatened → IBM drops 11%

The system checks:
1. Direct news for each watchlist stock
2. News from known competitors/disruptors
3. AI analyzes cross-company impact
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

import logging
import yfinance as yf
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Competitive Threat Map ───────────────────────────────────────────────────
# Maps watchlist stocks to companies whose news could threaten them.
# When we detect significant news from a DISRUPTOR, we flag the TARGET for analysis.
COMPETITIVE_THREAT_MAP = {
    # Legacy IT / Consulting
    "IBM": {
        "disruptors": ["MSFT", "GOOGL", "AMZN", "META", "anthropic", "openai"],
        "threat_keywords": [
            "COBOL", "mainframe", "legacy modernization", "consulting disruption",
            "AI replaces", "automation", "code migration", "enterprise AI",
            "Claude Code", "Copilot", "generative AI enterprise"
        ],
        "vulnerability": "IBM's consulting and legacy IT services threatened by AI automation",
    },
    # Cloud / Enterprise Software
    "MSFT": {
        "disruptors": ["GOOGL", "AMZN", "AAPL", "META", "anthropic", "openai"],
        "threat_keywords": ["Google Workspace", "AWS wins", "Azure outage", "antitrust", "OpenAI split"],
        "vulnerability": "Cloud market share and Office 365 subscriptions",
    },
    # Semiconductors
    "NVDA": {
        "disruptors": ["AMD", "INTC", "TSM", "GOOGL", "AMZN", "MSFT"],
        "threat_keywords": [
            "custom AI chip", "TPU", "Trainium", "Gaudi", "NVIDIA alternative",
            "export ban", "China chip", "ASICs replace GPU"
        ],
        "vulnerability": "GPU dominance in AI training challenged by custom silicon",
    },
    "AMD": {
        "disruptors": ["NVDA", "INTC", "QCOM", "AMZN"],
        "threat_keywords": ["NVIDIA dominance", "MI300 shortfall", "custom ASIC"],
        "vulnerability": "GPU market share vs NVIDIA",
    },
    # E-Commerce / Retail
    "AMZN": {
        "disruptors": ["MSFT", "GOOGL", "WMT", "SHOP"],
        "threat_keywords": ["AWS competitor", "Google Cloud wins", "retail disruption", "antitrust AWS"],
        "vulnerability": "AWS cloud market share, retail margin pressure",
    },
    # Social Media / Advertising
    "META": {
        "disruptors": ["GOOGL", "SNAP", "TikTok", "AAPL"],
        "threat_keywords": [
            "TikTok ban lifted", "Apple privacy", "ad revenue loss",
            "ATT impact", "Instagram decline", "Threads fails"
        ],
        "vulnerability": "Ad revenue dependent on data privacy rules",
    },
    # Electric Vehicles
    "TSLA": {
        "disruptors": ["BYD", "GM", "F", "RIVN", "NIO"],
        "threat_keywords": [
            "BYD outsells", "EV price war", "China EV", "robotaxi competitor",
            "Waymo beats", "autonomous vehicle"
        ],
        "vulnerability": "EV market share and FSD timeline",
    },
    # Gold / Safe Haven ETFs
    "GLD": {
        "disruptors": [],
        "threat_keywords": ["rate hike", "Fed hawkish", "dollar surge", "crypto replaces gold"],
        "vulnerability": "Rate hikes reduce gold appeal; strong dollar hurts gold",
    },
    "IAU": {
        "disruptors": [],
        "threat_keywords": ["rate hike", "Fed hawkish", "dollar surge"],
        "vulnerability": "Same as GLD - rate sensitivity",
    },
    "SLV": {
        "disruptors": [],
        "threat_keywords": ["industrial demand drop", "rate hike", "dollar surge", "recession"],
        "vulnerability": "Industrial demand + rate sensitivity",
    },
    # Crypto Proxies
    "MSTR": {
        "disruptors": [],
        "threat_keywords": ["Bitcoin crash", "BTC regulation", "SEC crypto", "Saylor sells"],
        "vulnerability": "Bitcoin price 1:1 correlation",
    },
    "COIN": {
        "disruptors": [],
        "threat_keywords": ["SEC lawsuit", "crypto ban", "exchange hack", "Binance wins"],
        "vulnerability": "Regulatory risk and crypto market volume",
    },
    # Payments / Gig Economy - 2028 GIC direct casualties
    "V": {
        "disruptors": [],
        "threat_keywords": [
            "stablecoin payment", "crypto payment", "AI agent bypass",
            "intelligence crisis", "ghost GDP", "white collar recession",
            "2028 global", "Citrini", "AI unemployment", "transaction decline",
            "interchange fee", "payment disruption", "Solana payments"
        ],
        "vulnerability": "AI agents routing payments via stablecoins, bypassing card networks; gig economy contraction reduces transaction volume",
    },
    "MA": {
        "disruptors": [],
        "threat_keywords": [
            "stablecoin payment", "crypto payment", "AI agent bypass",
            "intelligence crisis", "ghost GDP", "white collar recession",
            "2028 global", "payment disruption", "interchange decline"
        ],
        "vulnerability": "Same as Visa - AI agent payment routing threatens interchange revenue",
    },
    "JPM": {
        "disruptors": [],
        "threat_keywords": [
            "intelligence crisis", "ghost GDP", "white collar recession",
            "2028 global", "private credit crisis", "AI unemployment",
            "consumer default", "credit loss surge"
        ],
        "vulnerability": "Mass white-collar unemployment → loan defaults; AI disrupts banking services",
    },
    # Broad Market ETFs - affected by macro + 2028 GIC scenario
    "SPY": {
        "disruptors": [],
        "threat_keywords": [
            "recession", "Fed rate hike", "inflation surge", "credit crisis",
            "intelligence crisis", "ghost GDP", "2028 global", "Citrini",
            "S&P 500 crash", "AI displacement", "white collar recession",
            "market crash", "bear market"
        ],
        "vulnerability": "Macro recession / rate cycle risk + 2028 Global Intelligence Crisis scenario (S&P target 3,500)",
    },
    "QQQ": {
        "disruptors": [],
        "threat_keywords": [
            "tech bubble", "rate hike", "AI bubble", "antitrust big tech",
            "regulation tech", "intelligence crisis", "ghost GDP", "2028 global",
            "Citrini", "SaaS collapse", "seat-based model", "AI replaces software",
            "AI agent replaces SaaS", "white collar recession"
        ],
        "vulnerability": "Tech sector concentration + rate sensitivity + 2028 GIC SaaS extinction scenario",
    },
    "TQQQ": {
        "disruptors": [],
        "threat_keywords": [
            "volatility spike", "VIX surge", "market crash", "rate hike",
            "intelligence crisis", "2028 global", "tech selloff", "Citrini"
        ],
        "vulnerability": "3x leveraged - amplifies any QQQ downside; 2028 GIC scenario especially dangerous for leveraged positions",
    },
    "SOXL": {
        "disruptors": [],
        "threat_keywords": [
            "chip export ban", "semiconductor crash", "ASML restriction",
            "AI chip glut", "NVIDIA correction", "3x leveraged crash",
            "tariff", "trade war", "global tariff"
        ],
        "vulnerability": "3x leveraged semiconductor ETF - amplifies any chip sector downside; tariffs hurt TSMC/ASML supply chains",
    },
    # ── Gold / Silver ETFs: tariff & crisis beneficiaries ────────────────────
    "GLD": {
        "disruptors": [],
        "threat_keywords": [
            "tariff", "trade war", "global tariff", "Trump tariff",
            "recession", "inflation surge", "dollar weakness", "safe haven",
            "geopolitical risk", "market crash", "intelligence crisis"
        ],
        "vulnerability": "Gold rises on tariff/crisis fear — POSITIVE signal for GLD",
    },
    "IAU": {
        "disruptors": [],
        "threat_keywords": [
            "tariff", "trade war", "global tariff", "Trump tariff",
            "recession", "inflation surge", "dollar weakness", "safe haven",
            "geopolitical risk", "market crash", "intelligence crisis"
        ],
        "vulnerability": "Gold ETF rises on tariff/crisis fear — POSITIVE signal for IAU",
    },
    "SLV": {
        "disruptors": [],
        "threat_keywords": [
            "tariff", "trade war", "silver demand", "safe haven",
            "inflation hedge", "precious metals", "dollar collapse",
            "market crash", "recession"
        ],
        "vulnerability": "Silver ETF benefits from tariff inflation + industrial demand — POSITIVE signal for SLV",
    },
}

# Bonus: disruptors not in watchlist but whose news matters
DISRUPTOR_TICKERS = {
    "anthropic": None,  # Private company - monitor via news search on other stocks
    "openai": None,     # Private company - same
    "BYD": "BYDDY",     # BYD ADR
    "WMT": "WMT",
    "SHOP": "SHOP",
    "RIVN": "RIVN",
    "NIO": "NIO",
    "SNAP": "SNAP",
    "INTC": "INTC",
    "QCOM": "QCOM",
}


def fetch_recent_news(symbol: str, hours_back: int = 24) -> list:
    """Fetch recent news for a symbol from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        cutoff = datetime.utcnow() - timedelta(hours=hours_back)
        recent = []
        for item in news:
            pub_time = datetime.utcfromtimestamp(item.get("providerPublishTime", 0))
            if pub_time >= cutoff:
                recent.append({
                    "title": item.get("title", ""),
                    "publisher": item.get("publisher", ""),
                    "time": pub_time.isoformat(),
                    "symbol": symbol,
                })
        return recent
    except Exception as e:
        logger.debug(f"[NewsIntel] Could not fetch news for {symbol}: {e}")
        return []


def detect_threats_for_symbol(target_symbol: str, hours_back: int = 24) -> list:
    """
    Check if any disruptors of `target_symbol` have published threatening news.
    Returns list of detected threats with context.
    """
    config = COMPETITIVE_THREAT_MAP.get(target_symbol)
    if not config:
        return []

    threats = []
    keywords = [k.lower() for k in config["threat_keywords"]]

    # Check news from each disruptor
    disruptors_to_check = list(config["disruptors"])
    # Also check news ON the target itself for self-reported risks
    disruptors_to_check.append(target_symbol)

    for disruptor in disruptors_to_check:
        # Resolve to real ticker if needed
        ticker = DISRUPTOR_TICKERS.get(disruptor, disruptor)
        if ticker is None:
            continue  # Private company - skip direct fetch, rely on target's own news
        news_items = fetch_recent_news(ticker, hours_back)
        for item in news_items:
            title_lower = item["title"].lower()
            matched_keywords = [kw for kw in keywords if kw in title_lower]
            if matched_keywords:
                threats.append({
                    "target_symbol": target_symbol,
                    "disruptor": disruptor,
                    "news_title": item["title"],
                    "publisher": item["publisher"],
                    "time": item["time"],
                    "matched_keywords": matched_keywords,
                    "vulnerability": config["vulnerability"],
                    "threat_level": "HIGH" if len(matched_keywords) >= 2 else "MEDIUM",
                })

    return threats


def scan_all_threats(watchlist: list, hours_back: int = 24) -> dict:
    """
    Scan all watchlist stocks for competitive threats.
    Returns dict: symbol -> list of threats.
    """
    results = {}
    for symbol in watchlist:
        threats = detect_threats_for_symbol(symbol, hours_back)
        if threats:
            results[symbol] = threats
            for t in threats:
                logger.warning(
                    f"[NewsIntel] THREAT DETECTED: {symbol} threatened by '{t['news_title']}' "
                    f"(keywords: {t['matched_keywords']}) → Level: {t['threat_level']}"
                )
    return results


def build_threat_context(symbol: str, threats: list) -> str:
    """Build a context string for the AI about detected competitive threats."""
    if not threats:
        return ""
    lines = [f"### ⚠️ COMPETITIVE THREAT ALERTS for {symbol}"]
    for t in threats:
        lines.append(
            f"\n[{t['threat_level']}] Threat from {t['disruptor'].upper()}:\n"
            f"  News: \"{t['news_title']}\"\n"
            f"  Source: {t['publisher']} ({t['time'][:10]})\n"
            f"  Keywords: {', '.join(t['matched_keywords'])}\n"
            f"  Vulnerability: {t['vulnerability']}\n"
            f"  → INSTRUCTION: This news may negatively impact {symbol}. "
            f"Strongly consider recommending SELL if already holding, or avoid BUY."
        )
    return "\n".join(lines)


# ── Macro Scenario Detection ─────────────────────────────────────────────────
# High-conviction macro narratives that affect broad market positioning.
# When these scenarios gain traction in the news, the AI should adjust
# its risk posture across the entire portfolio.

MACRO_SCENARIOS = {
    "2028_global_intelligence_crisis": {
        "name": "2028 Global Intelligence Crisis",
        "description": (
            "Citrini Research scenario: rapid AI adoption causes white-collar mass unemployment "
            "→ 'Ghost GDP' (output without consumer spending) → deflationary depression. "
            "S&P 500 modeled to peak near 8,000 in 2026 then crash 38% to ~3,500. "
            "Michael Burry endorsed the bearish framing."
        ),
        "trigger_keywords": [
            "intelligence crisis", "ghost GDP", "white collar recession",
            "2028 global", "Citrini", "AI unemployment", "intelligence displacement",
            "agentic AI job loss", "seat-based SaaS extinction"
        ],
        "sectors_at_risk": ["SaaS", "Payments", "Gig Economy", "Private Credit", "Housing"],
        "stocks_to_avoid": ["V", "MA", "UBER", "DASH", "NOW", "CRM", "MDB", "COF", "KKR", "BX"],
        "potential_beneficiaries": ["NVDA", "GLD", "IAU", "SLV", "IBIT"],  # Compute owners + safe havens
        "severity": "CRITICAL",
    },
    "fed_rate_pause": {
        "name": "Fed Rate Pause / Pivot",
        "description": "Federal Reserve pauses or cuts rates, reducing discount rate for growth stocks.",
        "trigger_keywords": ["Fed pivot", "rate cut", "pause rate hike", "dovish Fed", "FOMC cut"],
        "sectors_at_risk": [],
        "stocks_to_avoid": [],
        "potential_beneficiaries": ["QQQ", "TQQQ", "NVDA", "MSFT", "AMZN", "TSLA"],
        "severity": "BULLISH",
    },
    "china_tech_decoupling": {
        "name": "US-China Tech Decoupling",
        "description": "Export bans, tariffs, or sanctions affecting semiconductor supply chains.",
        "trigger_keywords": ["export ban", "chip restriction", "TSMC sanction", "China decoupling", "tariff semiconductor"],
        "sectors_at_risk": ["Semiconductors", "AI Hardware"],
        "stocks_to_avoid": ["NVDA", "AMD", "ASML", "AVGO", "TSM", "SOXL"],
        "potential_beneficiaries": ["INTC"],
        "severity": "HIGH",
    },
    "trump_global_tariffs_2026": {
        "name": "Trump 2026 全球关税冲击",
        "description": (
            "特朗普宣布 15% 全球关税，2026年2月21日立即生效（继'解放日'关税后再度升级）。"
            "历史参考：2025年4月同类冲击导致 S&P 500 单周跌 10%+。"
            "关税推高通胀 → 美联储无法降息 → 压制增长股估值。"
            "黄金/白银/比特币为主要避险标的，出口依赖型科技和消费品股受压。"
        ),
        "trigger_keywords": [
            "tariff", "tariffs", "global tariff", "15% tariff", "10% tariff",
            "trade war", "import duty", "reciprocal tariff", "liberation day",
            "Trump tariff", "White House tariff", "tariff increase",
            "retaliatory tariff", "trade deficit", "protectionism",
        ],
        "sectors_at_risk": ["Consumer Discretionary", "Tech Hardware", "Retail", "Auto", "Industrials"],
        "stocks_to_avoid": ["AAPL", "TSLA", "AMZN", "META", "AVGO", "TSM", "ASML", "SOXL", "QQQ", "TQQQ"],
        "potential_beneficiaries": ["GLD", "IAU", "SLV", "IBIT", "XOM"],  # Gold / Silver / Oil safe havens
        "severity": "HIGH",
    },
}


def detect_active_macro_scenarios(hours_back: int = 6) -> list:
    """
    Scan recent financial news (via SPY, QQQ, general tickers) for macro scenario keywords.
    Returns list of active scenario names with evidence.
    """
    active = []
    # Use broad market ETFs as proxy for macro news coverage
    proxy_tickers = ["SPY", "QQQ", "VIX"]
    all_news = []
    for ticker in proxy_tickers:
        all_news.extend(fetch_recent_news(ticker, hours_back))

    for scenario_id, scenario in MACRO_SCENARIOS.items():
        keywords = [k.lower() for k in scenario["trigger_keywords"]]
        matched_items = []
        for item in all_news:
            title_lower = item["title"].lower()
            hits = [kw for kw in keywords if kw in title_lower]
            if hits:
                matched_items.append({"title": item["title"], "keywords": hits})

        if matched_items:
            active.append({
                "scenario_id": scenario_id,
                "name": scenario["name"],
                "severity": scenario["severity"],
                "description": scenario["description"],
                "evidence": matched_items[:3],
                "stocks_to_avoid": scenario["stocks_to_avoid"],
                "potential_beneficiaries": scenario["potential_beneficiaries"],
            })
            logger.warning(
                f"[MacroScenario] ACTIVE: '{scenario['name']}' — "
                f"{len(matched_items)} news item(s) matched keywords"
            )

    return active


def build_macro_scenario_context(active_scenarios: list) -> str:
    """Build AI context string for active macro scenarios."""
    if not active_scenarios:
        return ""
    lines = ["### 🌐 MACRO SCENARIO ALERTS"]
    for s in active_scenarios:
        severity_emoji = "🚨" if s["severity"] in ("CRITICAL", "HIGH") else "📈"
        lines.append(f"\n{severity_emoji} [{s['severity']}] {s['name']}")
        lines.append(f"  {s['description']}")
        lines.append(f"  Evidence ({len(s['evidence'])} articles):")
        for ev in s["evidence"][:2]:
            lines.append(f'    • "{ev["title"]}" → keywords: {ev["keywords"]}')
        if s["stocks_to_avoid"]:
            lines.append(f"  → AVOID / SELL: {', '.join(s['stocks_to_avoid'])}")
        if s["potential_beneficiaries"]:
            lines.append(f"  → CONSIDER: {', '.join(s['potential_beneficiaries'])}")
    lines.append(
        "\n  ⚠️ INSTRUCTION: Adjust portfolio risk exposure based on above macro scenarios. "
        "Reduce positions in 'avoid' stocks, consider rotating into beneficiaries."
    )
    return "\n".join(lines)
