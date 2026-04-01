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
import logging
import xml.etree.ElementTree as ET
import requests
import yfinance as yf
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

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
            "export ban", "China chip", "ASICs replace GPU",
            "Taalas", "MatX", "Groq chip", "Cerebras", "SambaNova",
            "Tenstorrent", "hardwired model", "inference chip startup",
            "model-as-silicon", "per-model silicon", "NVIDIA competitor",
            "challenge Nvidia", "beat Nvidia", "replace GPU"
        ],
        "vulnerability": "GPU dominance in AI training/inference challenged by custom silicon startups",
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


def _fetch_rss_news(symbol: str, hours_back: int = 24) -> list:
    """
    [Fallback] Fetch news via Yahoo Finance RSS when yfinance JSON API fails.
    No API key required; uses public RSS endpoint.
    """
    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={symbol}&region=US&lang=en-US"
    )
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    recent = []
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "AlphaTrader/1.0"})
        if resp.status_code != 200:
            logger.debug(f"[RSS] {symbol} HTTP {resp.status_code}")
            return []
        root = ET.fromstring(resp.text)
        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        channel = root.find("channel")
        if channel is None:
            return []
        for item in channel.findall("item"):
            title_el = item.find("title")
            pub_el = item.find("pubDate")
            creator_el = item.find("dc:creator", ns)
            if title_el is None or pub_el is None:
                continue
            try:
                pub_time = parsedate_to_datetime(pub_el.text).replace(tzinfo=None)
            except Exception:
                continue
            if pub_time < cutoff:
                continue
            recent.append({
                "title": title_el.text or "",
                "publisher": creator_el.text if creator_el is not None else "Yahoo Finance",
                "time": pub_time.isoformat(),
                "symbol": symbol,
                "source": "rss",
            })
    except Exception as e:
        logger.debug(f"[RSS] Could not fetch RSS for {symbol}: {e}")
    return recent


# ── Geopolitical RSS Sources ─────────────────────────────────────────────────
# Global news feeds that carry breaking geopolitical events:
# wars, sanctions, oil supply disruptions, central bank policy, etc.
GEOPOLITICAL_RSS_SOURCES = [
    # ── 美国政府 / 白宫 ──────────────────────────────────────────────────────
    {
        "name": "White House News",
        "url": "https://www.whitehouse.gov/feed/",
    },
    {
        "name": "White House Briefings",
        "url": "https://www.whitehouse.gov/briefing-room/feed/",
    },
    {
        "name": "US State Department",
        "url": "https://www.state.gov/rss-feed/press-releases/feed/",
    },
    {
        "name": "US Treasury",
        "url": "https://home.treasury.gov/news/press-releases/feed",
    },
    # ── 主流国际新闻 ────────────────────────────────────────────────────────
    {
        "name": "Reuters World",
        "url": "https://feeds.reuters.com/reuters/worldNews",
    },
    {
        "name": "Reuters Business",
        "url": "https://feeds.reuters.com/reuters/businessNews",
    },
    {
        "name": "BBC World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    {
        "name": "The Guardian World",
        "url": "https://www.theguardian.com/world/rss",
    },
    {
        "name": "NPR World",
        "url": "https://feeds.npr.org/1004/rss.xml",
    },
    {
        "name": "Financial Times",
        "url": "https://www.ft.com/rss/home/uk",
    },
    {
        "name": "Associated Press Top News",
        "url": "https://feeds.apnews.com/rss/apf-topnews",
    },
    # ── 中东 / 能源 专项 ────────────────────────────────────────────────────
    {
        "name": "Times of Israel",
        "url": "https://www.timesofisrael.com/feed/",
    },
    {
        "name": "Jerusalem Post",
        "url": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx",
    },
    {
        "name": "Oil Price News",
        "url": "https://oilprice.com/rss/main",
    },
    # ── 中国财经媒体 ─────────────────────────────────────────────────────────
    {
        "name": "新华社财经",
        "url": "http://www.xinhuanet.com/money/index.rss",
    },
    {
        "name": "证券时报",
        "url": "http://www.stcn.com/rss.xml",
    },
    {
        "name": "财经网",
        "url": "https://www.caijing.com.cn/rss/all.xml",
    },
    {
        "name": "第一财经",
        "url": "https://www.yicai.com/rss/news.xml",
    },
    # ── 亚洲财经新闻 ─────────────────────────────────────────────────────────
    {
        "name": "Nikkei Asia",
        "url": "https://asia.nikkei.com/rss/feed/nar",
    },
    {
        "name": "South China Morning Post",
        "url": "https://www.scmp.com/rss/91/feed",
    },
    {
        "name": "Economic Times India",
        "url": "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    },
    {
        "name": "Korea Times Business",
        "url": "https://www.koreatimes.co.kr/www2/rss/biztech.xml",
    },
    # ── 欧洲财经新闻 ─────────────────────────────────────────────────────────
    {
        "name": "Reuters Europe",
        "url": "https://feeds.reuters.com/reuters/europeanBusinessNews",
    },
    {
        "name": "Handelsblatt English",
        "url": "https://www.handelsblatt.com/contentexport/feed/english",
    },
    # ── 新兴市场 ─────────────────────────────────────────────────────────────
    {
        "name": "Bloomberg Asia Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
    },
    {
        "name": "Investing.com News",
        "url": "https://www.investing.com/rss/news.rss",
    },
]

# ── China-specific financial news (A-shares, macro policy) ───────────────────
CN_FINANCE_RSS_SOURCES = [
    {"name": "东方财富",   "url": "https://finance.eastmoney.com/rss/news.xml"},
    {"name": "同花顺财经", "url": "https://news.10jqka.com.cn/rss/index.xml"},
    {"name": "新浪财经",   "url": "https://finance.sina.com.cn/rss/finance.xml"},
    {"name": "中证网",     "url": "http://www.cs.com.cn/rss/csrss_finance.xml"},
    {"name": "上海证券报", "url": "https://www.cnstock.com/rss/news.xml"},
    {"name": "证监会公告", "url": "http://www.csrc.gov.cn/csrc/c101831/index.shtml"},
    {"name": "人民银行",   "url": "http://www.pbc.gov.cn/rss/index.rss"},
    {"name": "中国证监会", "url": "https://www.csrc.gov.cn/csrc/c100028/index.shtml"},
    {"name": "沪深交易所", "url": "https://www.sse.com.cn/news/"},
]

# ── Tech / Semiconductor RSS Sources ─────────────────────────────────────────
# Specialized feeds for chip industry, AI hardware, and startup news.
# Catches: new AI chip announcements, NVIDIA competitors, semiconductor supply chain.
TECH_RSS_SOURCES = [
    # ── 综合科技 ─────────────────────────────────────────────────────────────
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
    },
    {
        "name": "Ars Technica",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
    },
    {
        "name": "Wired",
        "url": "https://www.wired.com/feed/rss",
    },
    {
        "name": "MIT Technology Review",
        "url": "https://www.technologyreview.com/feed/",
    },
    # ── 半导体 / 芯片专项 ────────────────────────────────────────────────────
    {
        "name": "Tom's Hardware",
        "url": "https://www.tomshardware.com/feeds/all",
    },
    {
        "name": "IEEE Spectrum",
        "url": "https://spectrum.ieee.org/feeds/feed.rss",
    },
    {
        "name": "EE Times",
        "url": "https://www.eetimes.com/feed/",
    },
    {
        "name": "Semiconductor Engineering",
        "url": "https://semiengineering.com/feed/",
    },
    {
        "name": "The Register",
        "url": "https://www.theregister.com/headlines.atom",
    },
    # ── AI / 创业公司 ────────────────────────────────────────────────────────
    {
        "name": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
    },
    {
        "name": "The Outpost AI",
        "url": "https://theoutpost.ai/feed/",
    },
    {
        "name": "Turing Post",
        "url": "https://www.turingpost.com/feed",
    },
]

# ── Tech keyword → affected stock mapping ─────────────────────────────────────
# When these keywords appear in tech news, trigger re-analysis of the listed stocks.
TECH_KEYWORD_STOCK_MAP = {
    # AI chip startups threatening NVDA
    "Taalas":            ["NVDA", "AMD", "AVGO"],
    "MatX":              ["NVDA", "AMD"],
    "Groq":              ["NVDA", "AMD"],
    "Cerebras":          ["NVDA", "AMD"],
    "SambaNova":         ["NVDA", "AMD"],
    "Tenstorrent":       ["NVDA", "AMD"],
    "Etched":            ["NVDA"],
    "d-Matrix":          ["NVDA"],
    "Hailo":             ["NVDA"],
    # Hyperscaler custom silicon
    "Trainium":          ["NVDA", "AMD"],
    "Inferentia":        ["NVDA"],
    "Axion":             ["NVDA", "AMD", "INTC"],
    "Maia":              ["NVDA", "AMD"],
    "Graviton":          ["NVDA", "AMD", "INTC"],
    # TSMC supply chain
    "TSMC capacity":     ["NVDA", "AMD", "AVGO", "INTC"],
    "chip shortage":     ["NVDA", "AMD", "AVGO"],
    "CoWoS shortage":    ["NVDA"],
    "HBM shortage":      ["NVDA", "AMD"],
    # Export controls
    "export ban":        ["NVDA", "AMD", "AVGO"],
    "chip export":       ["NVDA", "AMD"],
    "BIS rule":          ["NVDA", "AMD"],
    # Semiconductor broadly
    "semiconductor":     ["NVDA", "AMD", "AVGO", "INTC", "TSM", "SOXL"],
    "AI chip":           ["NVDA", "AMD", "AVGO"],
    "inference chip":    ["NVDA", "AMD"],
    "custom silicon":    ["NVDA", "AMD", "GOOGL", "AMZN", "MSFT"],
    # Software / AI models → hardware demand
    "DeepSeek":          ["NVDA", "AMD"],
    "model training":    ["NVDA", "AMD"],
    "data center GPU":   ["NVDA", "AMD"],
}


def fetch_tech_news(hours_back: int = 12) -> list:
    """
    Fetch recent tech/semiconductor news from specialized RSS sources.
    Returns unified list of items with: title, publisher, url, time, source_name.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    all_items = []

    for source in TECH_RSS_SOURCES:
        try:
            resp = requests.get(
                source["url"], timeout=10,
                headers={"User-Agent": "AlphaTrader-TechNews/1.0"}
            )
            if resp.status_code != 200:
                logger.debug(f"[TechNews] {source['name']} HTTP {resp.status_code}")
                continue

            # Try feedparser first, fall back to raw XML
            try:
                import feedparser
                feed = feedparser.parse(resp.text)
                entries = feed.entries
            except Exception:
                entries = []

            if not entries:
                # Manual XML parse
                try:
                    root = ET.fromstring(resp.content)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    entries_raw = root.findall(".//item") or root.findall(".//atom:entry", ns)
                    for e in entries_raw:
                        title_el = e.find("title") or e.find("atom:title", ns)
                        link_el = e.find("link") or e.find("atom:link", ns)
                        date_el = e.find("pubDate") or e.find("atom:updated", ns) or e.find("atom:published", ns)
                        if title_el is None:
                            continue
                        title = title_el.text or ""
                        link = (link_el.text or link_el.get("href", "")) if link_el is not None else ""
                        pub_str = date_el.text if date_el is not None else ""
                        try:
                            pub_dt = parsedate_to_datetime(pub_str).replace(tzinfo=None)
                        except Exception:
                            pub_dt = datetime.utcnow()
                        if pub_dt >= cutoff:
                            all_items.append({
                                "title": title,
                                "publisher": source["name"],
                                "url": link,
                                "time": pub_dt.isoformat(),
                            })
                    continue
                except Exception:
                    continue

            for entry in entries:
                title = entry.get("title", "")
                link = entry.get("link", "")
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    try:
                        from calendar import timegm
                        pub_dt = datetime.utcfromtimestamp(timegm(pub))
                    except Exception:
                        pub_dt = datetime.utcnow()
                else:
                    pub_dt = datetime.utcnow()

                if pub_dt >= cutoff:
                    all_items.append({
                        "title": title,
                        "publisher": source["name"],
                        "url": link,
                        "time": pub_dt.isoformat(),
                    })

        except Exception as e:
            logger.debug(f"[TechNews] {source['name']} failed: {e}")

    logger.info(f"[TechNews] Fetched {len(all_items)} items from {len(TECH_RSS_SOURCES)} sources")
    return all_items


def detect_tech_market_impacts(hours_back: int = 2) -> list:
    """
    Scan tech RSS feeds for keyword hits and return list of impacted stocks.
    Each item: {keyword, title, publisher, url, time, affected_stocks, impact_level}
    """
    items = fetch_tech_news(hours_back=hours_back)
    impacts = []
    seen_titles = set()

    for item in items:
        title_lower = item["title"].lower()
        for keyword, stocks in TECH_KEYWORD_STOCK_MAP.items():
            if keyword.lower() in title_lower:
                if item["title"] in seen_titles:
                    continue
                seen_titles.add(item["title"])
                impacts.append({
                    "keyword": keyword,
                    "title": item["title"],
                    "publisher": item["publisher"],
                    "url": item.get("url", ""),
                    "time": item["time"],
                    "affected_stocks": stocks,
                    "impact_level": "HIGH" if any(
                        k in title_lower for k in ["challenge", "beat", "outperform", "replace", "rival"]
                    ) else "MEDIUM",
                })
                break  # one keyword match per article is enough

    if impacts:
        logger.info(f"[TechNews] {len(impacts)} tech market impact(s) detected")
    return impacts


def build_tech_impact_context(symbol: str, impacts: list) -> str:
    """Build AI context string for tech news impacts on a symbol."""
    relevant = [i for i in impacts if symbol in i["affected_stocks"]]
    if not relevant:
        return ""
    lines = [f"### 🔬 TECH/SEMICONDUCTOR NEWS ALERTS for {symbol}"]
    for imp in relevant[:5]:  # cap at 5 items
        lines.append(
            f"\n[{imp['impact_level']}] Keyword: '{imp['keyword']}'\n"
            f"  Headline: \"{imp['title']}\"\n"
            f"  Source: {imp['publisher']} ({imp['time'][:10]})\n"
            f"  → Monitor for competitive pressure on {symbol}."
        )
    return "\n".join(lines)


def fetch_geopolitical_news(hours_back: int = 12) -> list:
    """
    Fetch breaking geopolitical news from major global RSS sources.
    Returns unified list of news items with title, publisher, time.
    These feed into macro scenario detection to catch events like:
    - Wars, military strikes, sanctions
    - Oil supply disruptions (Strait of Hormuz, OPEC decisions)
    - Central bank announcements
    - Trade war escalations
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    all_items = []

    for source in GEOPOLITICAL_RSS_SOURCES:
        try:
            resp = requests.get(
                source["url"], timeout=10,
                headers={"User-Agent": "AlphaTrader-GeoNews/1.0"}
            )
            if resp.status_code != 200:
                logger.debug(f"[GeoNews] {source['name']} HTTP {resp.status_code}")
                continue

            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                # Some feeds have encoding issues; try stripping BOM
                cleaned = resp.text.encode("utf-8", errors="replace").decode("utf-8")
                root = ET.fromstring(cleaned)

            channel = root.find("channel")
            items_iter = channel.findall("item") if channel is not None else root.findall(".//item")

            count = 0
            for item in items_iter:
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                if title_el is None:
                    continue
                title = (title_el.text or "").strip()
                if not title:
                    continue

                pub_time = None
                if pub_el is not None and pub_el.text:
                    try:
                        pub_time = parsedate_to_datetime(pub_el.text).replace(tzinfo=None)
                    except Exception:
                        pass

                # If no pub date, include anyway (breaking news may lack dates)
                if pub_time and pub_time < cutoff:
                    continue

                all_items.append({
                    "title": title,
                    "publisher": source["name"],
                    "time": pub_time.isoformat() if pub_time else datetime.utcnow().isoformat(),
                    "symbol": "MACRO",
                    "source": "geopolitical_rss",
                })
                count += 1

            if count:
                logger.debug(f"[GeoNews] {source['name']}: {count} items")

        except Exception as e:
            logger.debug(f"[GeoNews] {source['name']} failed: {e}")

    logger.info(f"[GeoNews] Fetched {len(all_items)} geopolitical news items from {len(GEOPOLITICAL_RSS_SOURCES)} sources")
    return all_items


def fetch_cn_finance_news(hours_back: int = 6) -> list:
    """
    Fetch Chinese A-share market specific news from CN financial RSS sources.
    Catches: CSRC policy announcements, PBOC decisions, exchange notices,
             major A-share corporate actions, northbound/southbound capital flows.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    all_items = []

    for source in CN_FINANCE_RSS_SOURCES:
        try:
            resp = requests.get(
                source["url"], timeout=8,
                headers={"User-Agent": "AlphaTrader-CNFinance/1.0",
                         "Accept-Language": "zh-CN,zh;q=0.9"}
            )
            if resp.status_code != 200:
                continue
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError:
                continue
            channel = root.find("channel")
            items_iter = channel.findall("item") if channel is not None else root.findall(".//item")
            for item in items_iter:
                title_el = item.find("title")
                if title_el is None:
                    continue
                title = (title_el.text or "").strip()
                if not title:
                    continue
                all_items.append({
                    "title": title,
                    "publisher": source["name"],
                    "time": datetime.utcnow().isoformat(),
                    "region": "CN",
                })
        except Exception as e:
            logger.debug(f"[CNFinNews] {source['name']} failed: {e}")

    logger.info(f"[CNFinNews] Fetched {len(all_items)} items from {len(CN_FINANCE_RSS_SOURCES)} CN sources")
    return all_items


def fetch_global_market_news(hours_back: int = 6) -> dict:
    """
    Fetch news from all regions simultaneously.
    Returns dict: {region: [news_items]}
    Includes: geo news, CN finance news, tech news.
    """
    geo_news = fetch_geopolitical_news(hours_back=hours_back)
    cn_news = fetch_cn_finance_news(hours_back=hours_back)

    # Bucket by region keyword
    region_map = {
        "US": [], "CN": [], "HK": [], "JP": [], "EU": [],
        "APAC": [], "EM": [], "GLOBAL": [],
    }
    cn_keywords = ["中国", "A股", "上证", "深圳", "沪深", "茅台", "平安", "宁德",
                   "人民币", "央行", "证监会", "北向", "融资融券", "st股"]
    hk_keywords = ["hong kong", "hkex", "港股", "恒生", "腾讯", "阿里"]
    jp_keywords  = ["japan", "nikkei", "日本", "日经", "boj", "yen", "円"]
    eu_keywords  = ["europe", "ecb", "euro", "欧洲", "德国", "dax", "bund"]
    apac_keywords = ["korea", "australia", "singapore", "india", "asean",
                     "韩国", "澳大利亚", "新加坡", "印度"]
    em_keywords  = ["brazil", "turkey", "mexico", "indonesia", "vietnam", "russia",
                    "巴西", "土耳其", "墨西哥", "印尼", "越南", "俄罗斯"]
    us_keywords  = ["fed", "federal reserve", "nasdaq", "s&p", "dow", "wall street",
                    "美联储", "美股", "美元"]

    for item in geo_news + cn_news:
        t = (item.get("title") or "").lower()
        p = (item.get("publisher") or "")
        # CN finance sources → CN bucket
        if p in [s["name"] for s in CN_FINANCE_RSS_SOURCES] or any(k in t for k in cn_keywords):
            region_map["CN"].append(item)
        elif any(k in t for k in hk_keywords):
            region_map["HK"].append(item)
        elif any(k in t for k in jp_keywords):
            region_map["JP"].append(item)
        elif any(k in t for k in eu_keywords):
            region_map["EU"].append(item)
        elif any(k in t for k in apac_keywords):
            region_map["APAC"].append(item)
        elif any(k in t for k in em_keywords):
            region_map["EM"].append(item)
        elif any(k in t for k in us_keywords):
            region_map["US"].append(item)
        else:
            region_map["GLOBAL"].append(item)

    return region_map


def fetch_news_with_fallback(symbol: str, hours_back: int = 24) -> list:
    """
    Primary: yfinance.  Fallback: Yahoo Finance RSS.
    Always returns a list (empty if both sources fail).
    """
    results = fetch_recent_news(symbol, hours_back)
    if results:
        return results
    logger.info(f"[NewsIntel] yfinance returned 0 news for {symbol}, trying RSS fallback...")
    rss_results = _fetch_rss_news(symbol, hours_back)
    if rss_results:
        logger.info(f"[NewsIntel] RSS fallback returned {len(rss_results)} items for {symbol}")
    return rss_results


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
        news_items = fetch_news_with_fallback(ticker, hours_back)
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


# ── Sector Mapping (for RL Attribution) ──────────────────────────────────────
SYMBOL_SECTOR_MAP = {
    "GLD": "Gold", "IAU": "Gold", "SLV": "Silver",
    "XLE": "Energy", "USO": "Energy", "OXY": "Energy", "XOM": "Energy", "CVX": "Energy",
    "ITA": "Defense", "PPA": "Defense", "LMT": "Defense", "RTX": "Defense", "NOC": "Defense", "GD": "Defense",
    "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors", "INTC": "Semiconductors",
    "MSFT": "Software", "AAPL": "Tech", "GOOGL": "Tech", "AMZN": "Retail", "META": "Social Media",
    "TSLA": "EV", "BYD": "EV",
    "SPY": "Index", "QQQ": "Index", "IWM": "Index"
}

def get_symbol_sector(symbol: str) -> str:
    """Return the primary sector for a given ticker symbol."""
    return SYMBOL_SECTOR_MAP.get(symbol.upper(), "Other")

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
    "middle_east_war_2026": {
        "name": "中东战争 2026 — 美以联合打击伊朗",
        "description": (
            "2026年2月28日，美国与以色列联合对伊朗发动军事打击（代号 Operation Shield of Judah）。"
            "伊朗发动导弹/无人机反击，波及阿联酋、巴林、卡塔尔。"
            "霍尔木兹海峡风险：全球每日约2000万桶原油经此通过（占全球20%）。"
            "历史参考：类似中东冲突触发油价+15~30%，黄金+10~20%，科技股-5~15%。"
        ),
        "trigger_keywords": [
            "iran", "israel attack", "tehran", "strait of hormuz",
            "middle east war", "iran strike", "iran attack", "iranian missile",
            "operation shield", "us military iran", "preemptive strike iran",
            "iran retaliation", "iran nuclear", "iranian drone",
            "abu dhabi explosion", "bahrain strike", "gulf war",
            "oil supply disruption", "hormuz blockade", "persian gulf",
            "iran war", "israel iran", "netanyahu iran", "trump iran",
        ],
        "sectors_at_risk": ["Technology", "Airlines", "Consumer Discretionary", "Automotive"],
        "stocks_to_avoid": ["TSLA", "AMZN", "AAPL", "QQQ", "TQQQ", "SOXL"],
        "potential_beneficiaries": ["GLD", "IAU", "SLV", "XOM", "LMT", "RTX", "NOC"],
        "severity": "CRITICAL",
    },
    "hormuz_no_blockade_2026": {
        "name": "霍尔木兹海峡未封锁 — 能源溢价消退",
        "description": (
            "官方及多方消息确认霍尔木兹海峡航行安全，目前并无实际封锁发生。"
            "此前因担忧供应中断而推高的油价溢价（War Premium）正在迅速消退。"
            "能源股和石油相关ETF面临短期超额收益回吐风险。"
        ),
        "trigger_keywords": [
            "no blockade", "strait of hormuz open", "shipping safe",
            "not blockaded", "hormuz navigation normal", "strait secure",
            "oil flow unhindered", "no iran blockade", "hormuz tension eases",
            "油气板块跳水", "海峡没有封锁", "霍尔木兹通畅"
        ],
        "sectors_at_risk": ["Energy", "Petroleum", "Aerospace & Defense"],
        "stocks_to_avoid": ["XOM", "USO", "CVX", "OXY", "LMT", "RTX", "NOC", "UCO", "BNO"],
        "potential_beneficiaries": ["AAPL", "MSFT", "AMZN", "TSLA", "QQQ"],  # Tech recovery as oil falls
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
    "sector_overextension_risk": {
        "name": "Sector Over-extension Risk (High Points)",
        "description": (
            "Key sectors (Gold, Energy, Defense) are at multi-year highs or significantly "
            "overextended from their long-term means. Risk of 'mean reversion' or "
            "'sell-the-news' profit taking is high. Caution on chasing momentum here."
        ),
        "trigger_keywords": [
            "gold record", "oil peak", "defense stock high", "overextended",
            "overbought", "exhaustion gap", "52-week high", "all-time high",
            "parabolic move", "mean reversion risk"
        ],
        "sectors_at_risk": ["Precious Metals", "Energy", "Aerospace & Defense"],
        "stocks_to_avoid": ["GLD", "IAU", "XOM", "CVX", "LMT", "RTX", "NOC", "GD"],
        "potential_beneficiaries": ["SPY", "QQQ", "VIX"],
        "severity": "MEDIUM",
    },
}

# ── Auto-Watchlist Expansion Maps ────────────────────────────────────────────
# When a macro scenario activates, automatically add these tickers to watchlist
SCENARIO_AUTO_WATCHLIST: dict = {
    "middle_east_war_2026": [
        "USO", "UCO", "BNO",            # 原油ETF
        "FRO", "STNG", "NAT", "DHT",    # 油轮股（霍尔木兹封锁最大受益）
        "OXY", "CVX", "MRO",            # 石油生产商
        "NOC", "GD",                    # 防务扩展
        "GDX", "NEM",                   # 黄金矿业
    ],
    "trump_global_tariffs_2026": [
        "GDX", "NEM",                   # 黄金矿业
        "WMT", "COST",                  # 国内零售（进口替代受益）
        "DXY", "UUP",                   # 美元走强
    ],
    "china_tech_decoupling": [
        "INTC", "MRVL", "ON",           # 美国本土芯片
        "AMAT", "LRCX", "KLAC",         # 美国芯片设备
    ],
}

# 新闻关键词 → 自动添加标的（覆盖场景之外的突发事件）
NEWS_KEYWORD_AUTO_WATCHLIST: dict = {
    "hormuz": ["USO", "UCO", "FRO", "STNG", "BNO"],
    "oil blockade": ["USO", "UCO", "FRO", "BNO", "STNG"],
    "oil tanker": ["FRO", "STNG", "NAT", "DHT"],
    "crude oil spike": ["USO", "UCO", "XOM", "CVX", "OXY"],
    "gold record": ["GLD", "GDX", "NEM", "IAU"],
    "gold all-time": ["GLD", "GDX", "NEM"],
    "nuclear": ["CCJ", "NLR", "URA"],
    "cyber attack": ["CRWD", "PANW", "ZS", "FTNT"],
    "taiwan strait": ["GLD", "AMAT", "ASML", "LMT", "NOC"],
    "ukraine": ["GLD", "LMT", "RTX", "NOC", "OXY"],
    "bank collapse": ["GLD", "IBIT", "JPM"],
    "fed cut": ["TLT", "GLD", "IBIT"],
    "recession": ["GLD", "SLV", "TLT"],
    "semiconductor shortage": ["AMAT", "KLAC", "LRCX"],
    "bitcoin etf": ["IBIT", "MSTR", "COIN"],
    "debt ceiling": ["GLD", "SLV", "IBIT", "TLT"],
    "no blockade": ["USO", "XOM", "CVX", "OXY"],
    "strait secure": ["USO", "XOM"],
}


def get_watchlist_additions(
    active_scenarios: list,
    recent_news: list,
    current_watchlist: list,
) -> tuple:
    """
    Given active macro scenarios and recent news, return (new_tickers, reason_str).
    Only returns tickers NOT already in current_watchlist.
    """
    to_add: set = set()
    reasons: list = []
    current_set = set(current_watchlist)

    # 1. Scenario-based additions
    for scenario in active_scenarios:
        sid = scenario.get("scenario_id", "")
        additions = SCENARIO_AUTO_WATCHLIST.get(sid, [])
        new_for_scenario = [s for s in additions if s not in current_set and s not in to_add]
        if new_for_scenario:
            to_add.update(new_for_scenario)
            reasons.append(f"场景[{scenario['name']}] → {new_for_scenario}")

    # 2. News-keyword-based additions
    for item in recent_news:
        title_lower = item.get("title", "").lower()
        for keyword, tickers in NEWS_KEYWORD_AUTO_WATCHLIST.items():
            if keyword in title_lower:
                new_for_kw = [s for s in tickers if s not in current_set and s not in to_add]
                if new_for_kw:
                    to_add.update(new_for_kw)
                    reasons.append(f'关键词"{keyword}" → {new_for_kw}')

    reason_str = "; ".join(reasons) if reasons else ""
    return list(to_add), reason_str


def detect_active_macro_scenarios(hours_back: int = 6) -> list:
    """
    Scan recent financial news AND geopolitical RSS feeds for macro scenario keywords.
    Returns list of active scenario names with evidence.
    """
    active = []
    # Use broad market ETFs as proxy for macro/financial news
    proxy_tickers = ["SPY", "QQQ", "VIX", "GLD", "XOM"]
    all_news = []
    for ticker in proxy_tickers:
        all_news.extend(fetch_recent_news(ticker, hours_back))

    # Also scan geopolitical RSS feeds (Reuters, BBC, Al Jazeera, etc.)
    geo_news = fetch_geopolitical_news(hours_back=max(hours_back, 12))
    all_news.extend(geo_news)
    logger.info(f"[MacroScan] Scanning {len(all_news)} total news items ({len(geo_news)} geopolitical)")

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


def detect_technical_overextension(watchlist: list, db_session) -> list:
    """
    Check for stocks that are technically overextended (RSI > 80 or >25% above MA200).
    Returns a custom 'technical macro' scenario if many stocks are at high points.
    """
    import market_data as md
    overextended = []
    
    for symbol in watchlist:
        try:
            indicators = md.get_technical_indicators(symbol)
            if not indicators:
                continue
                
            rsi = indicators.get("rsi", 50)
            dist_ma200 = indicators.get("dist_from_ma200_pct", 0)
            
            # Criteria for 'High Point' over-extension
            is_high = rsi > 80 or dist_ma200 > 25
            
            if is_high:
                overextended.append({
                    "symbol": symbol,
                    "rsi": rsi,
                    "dist_ma200": dist_ma200,
                    "reason": "RSI > 80" if rsi > 80 else f"MA200 distance > 25% ({dist_ma200:.1f}%)"
                })
        except Exception:
            continue
            
    if not overextended:
        return []
        
    # If we have overextended stocks, return a synthetic scenario
    symbols = [o["symbol"] for o in overextended]
    evidence = [f"{o['symbol']} at high point: {o['reason']}" for o in overextended[:5]]
    
    return [{
        "scenario_id": "technical_overextension",
        "name": "Technical Over-extension (RSI/MA200 Extremes)",
        "severity": "MEDIUM",
        "description": "Multiple stocks in watchlist are hitting extreme technical overbought levels (RSI > 80 or >25% above MA200), suggesting a high probability of mean reversion or pullback.",
        "evidence": [{"title": e, "keywords": ["overextended"]} for e in evidence],
        "stocks_to_avoid": symbols,
        "potential_beneficiaries": ["VIX"],
    }]


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


# ── Fix 2: Positive Catalyst Map ─────────────────────────────────────────────
# Monitors for BULLISH events: large contracts, partnerships, earnings beats,
# product launches, regulatory approvals, etc.
# When matched, generates a BUY-leaning context for the AI.

CATALYST_MAP = {
    "AMD": {
        "catalyst_keywords": [
            "major contract", "partnership", "ai chip deal", "chip deployment",
            "gigawatt", "multi-year deal", "wins deal", "selected by", "chosen by",
            "supply agreement", "record revenue", "beats estimates", "beat expectations",
            "data center", "mi300", "mi350", "instinct", "hyperscaler",
            "meta amd", "google amd", "microsoft amd", "amazon amd",
        ],
        "upside_thesis": "AMD MI300/MI350 AI GPU adoption by hyperscalers; server CPU share gains vs Intel",
    },
    "NVDA": {
        "catalyst_keywords": [
            "blackwell", "gb200", "h100 sold out", "record datacenter", "beats estimates",
            "ai infrastructure", "sovereign ai", "new model", "major order",
        ],
        "upside_thesis": "NVIDIA Blackwell GPU cycle; sovereign AI infrastructure spending",
    },
    "META": {
        "catalyst_keywords": [
            "ai chip", "llama", "metaverse revenue", "record ad revenue", "beats estimates",
            "reels monetization", "whatsapp business", "ai assistant adoption",
        ],
        "upside_thesis": "Meta AI infrastructure + ad revenue acceleration via Reels/AI",
    },
    "MSFT": {
        "catalyst_keywords": [
            "copilot revenue", "azure growth", "ai contract", "openai deal",
            "beats estimates", "record cloud", "enterprise ai",
        ],
        "upside_thesis": "Azure AI growth driven by Copilot/OpenAI integration",
    },
    "GOOGL": {
        "catalyst_keywords": [
            "gemini adoption", "tpu", "cloud ai", "beats estimates", "search ai",
            "waymo revenue", "record ad", "cloud deal",
        ],
        "upside_thesis": "Google Cloud AI + Gemini monetization; TPU cost advantage",
    },
    "TSLA": {
        "catalyst_keywords": [
            "robotaxi launch", "fsd", "full self-driving", "cybercab", "record deliveries",
            "energy storage", "megapack", "beats estimates", "optimus robot",
        ],
        "upside_thesis": "Tesla FSD/robotaxi optionality + energy storage growth",
    },
    "NVDA": {
        "catalyst_keywords": [
            "blackwell", "gb200", "record datacenter", "beats estimates",
            "ai infrastructure spending", "sovereign ai", "new chip",
        ],
        "upside_thesis": "NVIDIA Blackwell GPU supercycle; AI training demand",
    },
    "AMZN": {
        "catalyst_keywords": [
            "aws record", "trainium", "inferentia", "ai cloud", "beats estimates",
            "prime growth", "record profit", "genai workload",
        ],
        "upside_thesis": "AWS AI workload growth + Trainium chip efficiency advantage",
    },
    "IBIT": {
        "catalyst_keywords": [
            "bitcoin etf inflow", "btc all time high", "institutional bitcoin",
            "corporate treasury bitcoin", "bitcoin adoption", "sec approval",
        ],
        "upside_thesis": "Bitcoin ETF inflows from institutional/corporate treasury demand",
    },
    "MSTR": {
        "catalyst_keywords": [
            "bitcoin purchase", "btc acquisition", "saylor", "bitcoin strategy",
            "bitcoin treasury", "btc all time high",
        ],
        "upside_thesis": "MicroStrategy leveraged Bitcoin accumulation strategy",
    },
    "AVGO": {
        "catalyst_keywords": [
            "custom asic", "xpu", "ai chip design", "hyperscaler contract",
            "google tpu", "meta asic", "apple chip", "beats estimates", "record networking",
        ],
        "upside_thesis": "Broadcom custom AI ASIC design wins at Google/Meta/Apple",
    },
    "GLD": {
        "catalyst_keywords": [
            "gold all time high", "central bank buying", "haven demand",
            "inflation hedge", "gold rally", "tariff fear", "recession fear",
            "dollar weakness", "fed cut expectations",
        ],
        "upside_thesis": "Gold safe-haven demand on macro uncertainty/tariffs/rate cuts",
    },
    "SLV": {
        "catalyst_keywords": [
            "silver rally", "industrial demand", "silver all time high",
            "solar panel demand", "precious metals", "inflation hedge",
        ],
        "upside_thesis": "Silver dual role: industrial demand (solar/EVs) + inflation hedge",
    },
    "TQQQ": {
        "catalyst_keywords": [
            "nasdaq rally", "tech rally", "rate cut", "fed pivot",
            "ai rally", "risk on", "growth stock rally",
        ],
        "upside_thesis": "3x leveraged NASDAQ - benefits from tech/AI bull market + rate cuts",
    },
    "SOXL": {
        "catalyst_keywords": [
            "semiconductor rally", "chip demand", "ai chip boom", "record orders",
            "tsmc capex", "chip act", "semiconductor upcycle",
        ],
        "upside_thesis": "3x leveraged semiconductor ETF - benefits from AI chip spending cycle",
    },
    "XOM": {
        "catalyst_keywords": [
            "oil price surge", "crude rally", "brent rises", "oil supply disruption",
            "hormuz", "opec cut", "iran war", "middle east conflict", "energy rally",
            "record profit", "beats estimates", "upstream growth", "lng demand",
        ],
        "upside_thesis": "ExxonMobil benefits from oil price spikes driven by Middle East conflict/OPEC cuts",
    },
    "LMT": {
        "catalyst_keywords": [
            "defense contract", "pentagon contract", "military spending", "nato",
            "war", "conflict", "f-35", "missile defense", "hypersonic",
            "ukraine weapons", "israel weapons", "iran strike", "defense budget",
            "record contract", "billion contract",
        ],
        "upside_thesis": "Lockheed Martin benefits from increased defense spending during geopolitical conflicts",
    },
    "RTX": {
        "catalyst_keywords": [
            "defense contract", "raytheon missile", "patriot missile", "iron dome",
            "air defense", "military spending", "nato", "war", "conflict",
            "pentagon", "ukraine", "israel defense", "iran strike",
        ],
        "upside_thesis": "RTX (Raytheon) benefits from missile/air defense demand in Middle East conflicts",
    },
}


# ── Next-Day Buy Rules (Event-Driven) ────────────────────────────────────────
# These are specific, high-impact catalysts that we want to buy on the next
# market open (e.g., Meta buying AMD chips; NVDA earnings beat).
NEXT_DAY_BUY_RULES = {
    "AMD": {
        "title_keywords_any": [
            "meta", "facebook", "instagram"
        ],
        "title_keywords_all": [
            ["amd", "chip"],
            ["amd", "gpu"],
            ["amd", "mi300"],
            ["amd", "instinct"],
            ["amd", "ai chip"],
            ["amd", "accelerator"],
            ["amd", "order"],
            ["amd", "purchase"],
            ["amd", "buy"],
            ["amd", "deal"],
        ],
        "extra_symbols_to_check": ["META"],
        "reason": "Meta procurement/partnership signals demand acceleration for AMD AI GPUs.",
    },
    "NVDA": {
        "title_keywords_any": [
            "earnings", "results", "quarter", "guidance"
        ],
        "title_keywords_all": [
            ["beat", "expectations"],
            ["beats", "expectations"],
            ["beat", "estimates"],
            ["beats", "estimates"],
            ["tops", "estimates"],
            ["raises", "guidance"],
            ["guidance", "raised"],
            ["outlook", "raised"],
        ],
        "extra_symbols_to_check": [],
        "reason": "Earnings beat or raised guidance tends to drive strong next-day momentum.",
    },
}


def detect_next_day_buy_signals(target_symbol: str, hours_back: int = 24) -> list:
    """
    Detect high-impact catalysts that should trigger a next-market-open BUY.
    Returns list of signals with title + reason.
    """
    rule = NEXT_DAY_BUY_RULES.get(target_symbol)
    if not rule:
        return []

    symbols_to_check = [target_symbol] + rule.get("extra_symbols_to_check", [])
    seen_titles = set()
    signals = []

    for sym in symbols_to_check:
        news_items = fetch_news_with_fallback(sym, hours_back)
        for item in news_items:
            title = (item.get("title") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            title_lower = title.lower()
            any_ok = any(kw in title_lower for kw in rule["title_keywords_any"])
            if not any_ok:
                continue

            all_ok = any(
                all(k in title_lower for k in group)
                for group in rule["title_keywords_all"]
            )
            if not all_ok:
                continue

            signals.append({
                "target_symbol": target_symbol,
                "news_title": title,
                "publisher": item.get("publisher", ""),
                "time": item.get("time", ""),
                "source": item.get("source", "yfinance"),
                "reason": rule["reason"],
                "matched_any": [kw for kw in rule["title_keywords_any"] if kw in title_lower],
            })

    if signals:
        for s in signals:
            logger.info(
                f"[NextDayBuy] TRIGGER: {target_symbol} — \"{s['news_title'][:70]}\" "
                f"(source: {s['source']})"
            )

    return signals


def detect_catalysts_for_symbol(target_symbol: str, hours_back: int = 24) -> list:
    """
    Check if there is positive catalyst news for `target_symbol`.
    Returns list of detected catalysts with strength scoring.
    
    Unlike detect_threats_for_symbol(), this looks for BULLISH signals
    such as large contracts, partnerships, earnings beats, product launches.
    """
    config = CATALYST_MAP.get(target_symbol)
    if not config:
        return []

    catalysts = []
    keywords = [k.lower() for k in config["catalyst_keywords"]]

    # Fetch news for the target stock itself (direct catalysts)
    news_items = fetch_news_with_fallback(target_symbol, hours_back)

    for item in news_items:
        title_lower = item["title"].lower()
        matched_keywords = [kw for kw in keywords if kw in title_lower]
        if matched_keywords:
            strength = len(matched_keywords)
            catalysts.append({
                "target_symbol": target_symbol,
                "news_title": item["title"],
                "publisher": item.get("publisher", ""),
                "time": item["time"],
                "matched_keywords": matched_keywords,
                "upside_thesis": config["upside_thesis"],
                "strength": strength,
                "catalyst_level": "STRONG" if strength >= 3 else "MEDIUM" if strength >= 2 else "MILD",
                "source": item.get("source", "yfinance"),
            })

    if catalysts:
        for c in catalysts:
            logger.info(
                f"[CatalystMap] 🚀 CATALYST DETECTED: {target_symbol} — \"{c['news_title'][:60]}\" "
                f"(keywords: {c['matched_keywords']}) → Level: {c['catalyst_level']}"
            )

    return catalysts


def build_catalyst_context(symbol: str, catalysts: list) -> str:
    """Build a BUY-leaning context string for the AI about detected positive catalysts."""
    if not catalysts:
        return ""

    lines = [f"### 🚀 POSITIVE CATALYST ALERTS for {symbol}"]
    for c in catalysts:
        lines.append(
            f"\n[{c['catalyst_level']}] Positive Catalyst Detected:\n"
            f"  News: \"{c['news_title']}\"\n"
            f"  Source: {c['publisher']} ({c['time'][:10]})\n"
            f"  Keywords matched: {', '.join(c['matched_keywords'])}\n"
            f"  Thesis: {c['upside_thesis']}\n"
            f"  → INSTRUCTION: This is a BULLISH signal for {symbol}. "
            f"Strongly consider BUY if not already positioned. "
            f"This catalyst may outweigh general macro headwinds."
        )
    return "\n".join(lines)


# ── Fix 3: Catalyst vs Macro Priority Resolution ──────────────────────────────
# When a strong individual stock catalyst CONFLICTS with a macro bearish scenario,
# this function produces a priority note for the AI to weigh correctly.
#
# Override Rules (conservative by design):
#   MILD catalyst    (1 kw match)  → cannot override any macro scenario
#   MEDIUM catalyst  (2 kw match)  → can override LOW severity macro scenarios
#   STRONG catalyst  (3+ kw match) → can override MEDIUM/HIGH macro scenarios
#                                    but NOT CRITICAL (e.g. 2028 GIC)

_MACRO_SEVERITY_RANK = {
    "BULLISH": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}


def resolve_signal_priority(symbol: str, catalysts: list, active_macros: list) -> str:
    """
    Produce a priority-resolution note for the AI when catalysts conflict with macros.

    Returns an instruction string injected into the AI context, helping it decide
    whether to follow the macro bias or the individual catalyst signal.
    """
    if not catalysts or not active_macros:
        return ""

    # Find the highest-strength catalyst for this symbol
    best_catalyst = max(catalysts, key=lambda c: c["strength"])
    cat_strength = best_catalyst["strength"]
    cat_level = best_catalyst["catalyst_level"]  # MILD / MEDIUM / STRONG

    # Find macro scenarios that list this symbol as one to avoid
    conflicting_macros = [
        m for m in active_macros
        if symbol in m.get("stocks_to_avoid", [])
    ]
    if not conflicting_macros:
        # No conflict — catalyst is purely additive
        return (
            f"### ✅ PRIORITY NOTE for {symbol}\n"
            f"A [{cat_level}] catalyst was detected with no conflicting macro scenario. "
            f"The bullish catalyst signal is ADDITIVE — weight it alongside technical analysis."
        )

    # Find the most severe conflicting macro
    worst_macro = max(
        conflicting_macros,
        key=lambda m: _MACRO_SEVERITY_RANK.get(m.get("severity", "LOW"), 1)
    )
    macro_severity = worst_macro.get("severity", "LOW")
    macro_rank = _MACRO_SEVERITY_RANK.get(macro_severity, 1)

    # Apply override rules
    lines = [f"### ⚖️ SIGNAL CONFLICT RESOLUTION for {symbol}"]
    lines.append(
        f"  MACRO SCENARIO: [{macro_severity}] {worst_macro['name']} lists {symbol} as AVOID."
    )
    lines.append(
        f"  INDIVIDUAL CATALYST: [{cat_level}] \"{best_catalyst['news_title'][:70]}\" "
        f"({len(best_catalyst['matched_keywords'])} keyword matches)"
    )

    if macro_severity == "CRITICAL":
        lines.append(
            f"  → VERDICT: MACRO WINS. The {macro_severity} scenario is systemic and "
            f"cannot be overridden by individual catalysts. HOLD or exercise caution on {symbol}."
        )
    elif cat_strength >= 3 and macro_rank <= 3:  # STRONG catalyst vs HIGH or lower
        lines.append(
            f"  → VERDICT: CATALYST OVERRIDES MACRO. The {cat_level} catalyst "
            f"({cat_strength} keyword matches) is significant enough to override the {macro_severity} "
            f"macro headwind for {symbol} specifically. Consider a TACTICAL BUY with tight stop-loss "
            f"(the macro risk still exists as a broader backdrop)."
        )
    elif cat_strength >= 2 and macro_rank <= 2:  # MEDIUM catalyst vs MEDIUM or lower
        lines.append(
            f"  → VERDICT: PARTIAL OVERRIDE. The catalyst partially offsets the {macro_severity} "
            f"macro concern. Consider a REDUCED POSITION (50% of normal size) in {symbol}."
        )
    else:
        lines.append(
            f"  → VERDICT: MACRO WINS. The catalyst ({cat_level}, {cat_strength} kw) is not "
            f"strong enough to override the {macro_severity} macro scenario. HOLD {symbol} for now."
        )

    return "\n".join(lines)


# ── Self-Restructuring Catalyst Detection ────────────────────────────────────
# Key insight: when a company announces its OWN layoffs/restructuring, the market
# typically reacts POSITIVELY (cost reduction → margin expansion → EPS beats).
# This is the opposite of how we treat "company X lays off, bad for sector".
# Oracle +6% on layoffs is the canonical example.

_RESTRUCTURING_KEYWORDS = [
    "layoff", "layoffs", "laid off", "job cuts", "cut jobs",
    "workforce reduction", "headcount reduction", "restructuring",
    "cost reduction plan", "right-sizing", "streamlining workforce",
    "eliminat", "trimming staff", "reduce headcount",
]

# Ticker → company name fragments (for matching news headlines to the right company)
# Covers major global tech companies — layoff/restructuring by these = BUY signal for that ticker
_TICKER_NAME_MAP = {
    # ── US Big Tech ───────────────────────────────────────────────────────────
    "AAPL":  ["apple"],
    "MSFT":  ["microsoft"],
    "GOOGL": ["google", "alphabet"],
    "AMZN":  ["amazon"],
    "META":  ["meta ", "facebook", "instagram", "whatsapp"],
    "TSLA":  ["tesla"],
    "NVDA":  ["nvidia"],
    "AMD":   ["advanced micro devices", " amd "],
    "INTC":  ["intel"],
    "QCOM":  ["qualcomm"],
    "AVGO":  ["broadcom"],
    "MU":    ["micron"],
    # ── US Enterprise / Cloud ─────────────────────────────────────────────────
    "ORCL":  ["oracle"],
    "CRM":   ["salesforce"],
    "NOW":   ["servicenow"],
    "ADBE":  ["adobe"],
    "IBM":   ["ibm"],
    "INTU":  ["intuit"],
    "WDAY":  ["workday"],
    "SAP":   ["sap "],
    "CSCO":  ["cisco"],
    "ACN":   ["accenture"],
    "CTSH":  ["cognizant"],
    "CDNS":  ["cadence"],
    "SNPS":  ["synopsys"],
    "FISV":  ["fiserv"],
    # ── US Internet / Consumer Tech ───────────────────────────────────────────
    "NFLX":  ["netflix"],
    "UBER":  ["uber"],
    "LYFT":  ["lyft"],
    "SNAP":  ["snap "],
    "PINS":  ["pinterest"],
    "PYPL":  ["paypal"],
    "EBAY":  ["ebay"],
    "ABNB":  ["airbnb"],
    "DASH":  ["doordash"],
    "SPOT":  ["spotify"],
    "COIN":  ["coinbase"],
    "RBLX":  ["roblox"],
    # ── US Hardware / Semi ────────────────────────────────────────────────────
    "DELL":  ["dell"],
    "HPE":   ["hewlett packard enterprise", " hpe"],
    "HPQ":   ["hp inc", "hewlett-packard"],
    "AMAT":  ["applied materials"],
    "LRCX":  ["lam research"],
    "TXN":   ["texas instruments"],
    # ── China / HK Tech (US-listed ADR) ──────────────────────────────────────
    "BABA":  ["alibaba", "aliyun", "taobao", "tmall", "ant group"],
    "BIDU":  ["baidu"],
    "JD":    ["jd.com", "jingdong", "jd logistics"],
    "PDD":   ["pinduoduo", "temu"],
    "TCEHY": ["tencent", "wechat", "weixin"],
    "NTES":  ["netease"],
    "BILI":  ["bilibili"],
    "IQ":    ["iqiyi"],
    "TCOM":  ["trip.com", "ctrip"],
    "BEKE":  ["ke holdings", "beike", "lianjia"],
    # ── Taiwan ───────────────────────────────────────────────────────────────
    "TSM":   ["tsmc", "taiwan semiconductor"],
    "ASML":  ["asml"],
    # ── Korea ────────────────────────────────────────────────────────────────
    "005930.KS": ["samsung"],
    "000660.KS": ["sk hynix", "hynix"],
    "035420.KS": ["naver"],
    "035720.KS": ["kakao"],
    # ── Japan ────────────────────────────────────────────────────────────────
    "SONY":  ["sony"],
    "NTDOY": ["nintendo"],
    "SFTBY": ["softbank"],
    "9984.T":  ["softbank"],
    "6758.T":  ["sony"],
    "6501.T":  ["hitachi"],
    "6702.T":  ["fujitsu"],
    "6752.T":  ["panasonic"],
    "4689.T":  ["yahoo japan", "z holdings", "lyd"],
    "3659.T":  ["nexon"],
    # ── Telecom (global) ─────────────────────────────────────────────────────
    "ERIC":  ["ericsson"],
    "NOK":   ["nokia"],
    # ── India IT ─────────────────────────────────────────────────────────────
    "INFY":      ["infosys"],
    "WIT":       ["wipro"],
    "INFY.NS":   ["infosys"],
    "TCS.NS":    ["tata consultancy", " tcs"],
    "HCLTECH.NS":["hcl tech"],
    "TECHM.NS":  ["tech mahindra"],
    # ── Europe Tech ──────────────────────────────────────────────────────────
    "SAP.DE":   ["sap "],
    "ASML.AS":  ["asml"],
    "CAP.PA":   ["capgemini"],
    "SIE.DE":   ["siemens"],
    # ── Canada ───────────────────────────────────────────────────────────────
    "SHOP":  ["shopify"],
    "BB":    ["blackberry"],
    # ── Southeast Asia ───────────────────────────────────────────────────────
    "SE":    ["sea limited", "shopee", "garena"],
    "GRAB":  ["grab "],
}


def detect_restructuring_catalysts(symbols: list, hours_back: int = 48) -> list:
    """
    Scan recent news for symbols that announced their OWN layoffs/restructuring.
    Returns a list of dicts: {symbol, headline, publisher, timestamp, strength}

    'strength' is 1-3:
      1 = minor restructuring mention
      2 = explicit job cuts with percentage/headcount
      3 = large-scale (>5% workforce or >5000 employees)

    This generates a BULLISH signal for the announcing company because:
    - Cost reduction → operating leverage improves
    - Market typically rewards discipline over growth-at-all-costs
    - Oracle +6% on layoffs is a real example of this pattern
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    results = []

    for symbol in symbols:
        name_frags = _TICKER_NAME_MAP.get(symbol.upper(), [symbol.lower()])
        try:
            news = yf.Ticker(symbol).news or []
        except Exception:
            continue

        for item in news:
            title = (item.get("title") or "").lower()
            if not title:
                continue

            # Must mention the company itself (not just industry news)
            if not any(frag in title for frag in name_frags) and symbol.lower() not in title:
                continue

            # Must contain restructuring keyword
            matched_kws = [k for k in _RESTRUCTURING_KEYWORDS if k in title]
            if not matched_kws:
                continue

            pub_ts = item.get("providerPublishTime", 0) or 0
            try:
                pub_dt = datetime.utcfromtimestamp(pub_ts)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue

            # Score strength
            strength = 1
            if any(k in title for k in ["job cuts", "workforce reduction", "headcount reduction", "laid off"]):
                strength = 2
            if any(k in title for k in ["%", "thousand", "workers", "employees"]):
                strength = 3

            results.append({
                "symbol": symbol.upper(),
                "headline": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "timestamp": pub_dt.isoformat(),
                "matched_keywords": matched_kws,
                "strength": strength,
                "context": (
                    f"### RESTRUCTURING CATALYST\n"
                    f"{symbol.upper()} announced its own layoffs/restructuring. "
                    f"Historically, self-imposed cost-cutting is BULLISH for the announcing company "
                    f"(cost reduction → margin expansion → EPS upside). "
                    f"Headline: \"{item.get('title', '')}\"\n"
                    f"Signal strength: {strength}/3. "
                    f"Consider a BUY if technicals confirm and the stock hasn't already spiked >5%."
                ),
            })
            break  # one match per symbol is enough

    logger.info(f"[Restructuring] Scanned {len(symbols)} symbols, found {len(results)} restructuring catalysts")
    return results
