"""
Blog Monitor - Official AI Company Blog Surveillance
=====================================================
Monitors official blogs from major AI companies (Anthropic, OpenAI, Google, etc.)
via RSS/Atom feeds. These first-party announcements are the EARLIEST signal of
competitive disruption — often appearing hours before financial news covers them.

Classic example:
  Anthropic blog: "Claude Code automates COBOL modernization"
  → IBM consulting revenue directly threatened
  → IBM stock drops 13% within hours

We detect these first.
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "AlphaTrader/1.0 (financial research bot)",
    "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
}

# ── Official Blog RSS Feeds ───────────────────────────────────────────────────
# These are first-party sources. When a major AI lab publishes here,
# it's confirmed, authoritative, and typically moves markets.
BLOG_FEEDS = {
    "anthropic": {
        "name": "Anthropic",
        "rss_url": "https://www.anthropic.com/rss.xml",
        "fallback_url": "https://www.anthropic.com/news",
        "company_type": "AI Lab",
    },
    "openai": {
        "name": "OpenAI",
        "rss_url": "https://openai.com/blog/rss.xml",
        "fallback_url": "https://openai.com/news",
        "company_type": "AI Lab",
    },
    "google_ai": {
        "name": "Google AI Blog",
        "rss_url": "https://blog.google/technology/ai/rss/",
        "fallback_url": "https://blog.google/technology/ai/",
        "company_type": "AI Lab",
    },
    "google_deepmind": {
        "name": "Google DeepMind",
        "rss_url": "https://deepmind.google/blog/rss.xml",
        "fallback_url": "https://deepmind.google/discover/blog/",
        "company_type": "AI Lab",
    },
    "microsoft_ai": {
        "name": "Microsoft AI Blog",
        "rss_url": "https://blogs.microsoft.com/ai/feed/",
        "fallback_url": "https://blogs.microsoft.com/ai/",
        "company_type": "Tech Giant",
    },
    "meta_ai": {
        "name": "Meta AI",
        "rss_url": "https://ai.meta.com/blog/feed/",
        "fallback_url": "https://ai.meta.com/blog/",
        "company_type": "AI Lab",
    },
    "aws_ml": {
        "name": "AWS Machine Learning Blog",
        "rss_url": "https://aws.amazon.com/blogs/machine-learning/feed/",
        "fallback_url": "https://aws.amazon.com/blogs/machine-learning/",
        "company_type": "Cloud Provider",
    },
    "nvidia_blog": {
        "name": "NVIDIA Technical Blog",
        "rss_url": "https://developer.nvidia.com/blog/feed/",
        "fallback_url": "https://developer.nvidia.com/blog/",
        "company_type": "Semiconductor",
    },
}

# ── Blog Topic → Stock Impact Map ────────────────────────────────────────────
# When a blog post matches these keywords, these stocks are impacted.
# Format: keyword_group → {stocks_to_avoid, stocks_to_buy, severity, reason}
BLOG_IMPACT_MAP = [
    # ── COBOL / Legacy IT Modernization ──────────────────────────────────────
    {
        "keywords": ["COBOL", "mainframe", "legacy modernization", "legacy code", "COBOL migration"],
        "stocks_to_avoid": ["IBM"],
        "stocks_to_watch": ["MSFT", "GOOGL", "AMZN"],  # Cloud migration beneficiaries
        "severity": "HIGH",
        "reason": "AI automation of COBOL/legacy migration directly threatens IBM Consulting revenue (~$20B/yr from legacy services)",
        "sector": "Legacy IT / Consulting",
    },
    # ── Enterprise AI / SaaS Displacement ────────────────────────────────────
    {
        "keywords": [
            "enterprise AI agent", "AI automates", "replace software",
            "autonomous coding", "AI replaces developer", "multi-agent enterprise",
            "agentic workflow", "AI automates workflow"
        ],
        "stocks_to_avoid": ["IBM", "NOW", "CRM", "MDB", "ORCL", "SAP"],
        "stocks_to_watch": ["NVDA", "MSFT", "GOOGL", "AMZN"],
        "severity": "HIGH",
        "reason": "Enterprise AI agents replace seat-based SaaS subscriptions (ServiceNow, Salesforce model under threat)",
        "sector": "Enterprise SaaS",
    },
    # ── Custom AI Chips / GPU Alternative ────────────────────────────────────
    {
        "keywords": [
            "custom chip", "AI accelerator", "TPU", "Trainium", "Gaudi",
            "in-house silicon", "custom silicon", "ASIC", "neural processor"
        ],
        "stocks_to_avoid": ["NVDA", "AMD"],
        "stocks_to_watch": ["GOOGL", "AMZN", "MSFT"],  # Cloud cos making own chips
        "severity": "MEDIUM",
        "reason": "Major tech companies building custom AI chips reduce dependency on NVIDIA GPUs",
        "sector": "AI Hardware / Semiconductors",
    },
    # ── Autonomous / Robotic Vehicles ─────────────────────────────────────────
    {
        "keywords": [
            "autonomous vehicle", "self-driving", "robotaxi", "waymo launch",
            "autonomous driving", "FSD competitor"
        ],
        "stocks_to_avoid": ["TSLA"],  # Competition reduces Tesla FSD moat
        "stocks_to_watch": ["GOOGL"],  # Waymo parent
        "severity": "MEDIUM",
        "reason": "Competing AV milestones erode Tesla's FSD premium and differentiation",
        "sector": "Autonomous Vehicles",
    },
    # ── AI in Financial Services ──────────────────────────────────────────────
    {
        "keywords": [
            "AI banking", "AI financial advisor", "automated trading AI",
            "AI replaces analyst", "AI wealth management", "AI lending",
            "AI credit scoring", "autonomous finance"
        ],
        "stocks_to_avoid": ["JPM", "GS", "MS", "V", "MA"],
        "stocks_to_watch": ["NVDA", "MSFT"],
        "severity": "MEDIUM",
        "reason": "AI disrupts traditional financial advisory and credit underwriting revenue",
        "sector": "Financial Services",
    },
    # ── Payments / Stablecoin / Crypto Rail ──────────────────────────────────
    {
        "keywords": [
            "stablecoin payments", "crypto payment rail", "AI agent payment",
            "bypass card network", "programmable money", "on-chain payment",
            "USDC enterprise", "payment without intermediary"
        ],
        "stocks_to_avoid": ["V", "MA", "COF", "AXP"],
        "stocks_to_watch": ["COIN", "IBIT"],
        "severity": "HIGH",
        "reason": "Stablecoins and AI agent native payments bypass Visa/Mastercard interchange fees",
        "sector": "Payments",
    },
    # ── AI Drug Discovery / Biotech ───────────────────────────────────────────
    {
        "keywords": [
            "AI drug discovery", "protein folding", "AlphaFold", "AI pharma",
            "autonomous biology", "AI clinical trial"
        ],
        "stocks_to_avoid": [],
        "stocks_to_watch": ["NVDA", "GOOGL", "AMZN"],  # Infrastructure for biotech AI
        "severity": "LOW",
        "reason": "AI biotech breakthroughs benefit AI compute providers",
        "sector": "Biotech / AI",
    },
    # ── AI Model Cost Reduction ────────────────────────────────────────────────
    {
        "keywords": [
            "cheaper inference", "model efficiency", "distillation", "smaller model",
            "open source model", "free model", "open weights"
        ],
        "stocks_to_avoid": ["NVDA"],  # Fewer GPUs needed per inference
        "stocks_to_watch": [],
        "severity": "MEDIUM",
        "reason": "Model efficiency breakthroughs reduce GPU demand per unit of AI output",
        "sector": "AI Infrastructure",
    },
    # ── OpenAI / Anthropic New Model Release ──────────────────────────────────
    {
        "keywords": [
            "GPT-5", "Claude 4", "Gemini Ultra", "new model", "model release",
            "AGI", "reasoning model", "o3", "o4", "Claude opus"
        ],
        "stocks_to_avoid": [],
        "stocks_to_watch": ["NVDA", "MSFT", "GOOGL", "AMZN"],  # More GPU demand
        "severity": "MEDIUM",
        "reason": "Major model releases drive increased AI infrastructure spending",
        "sector": "AI Models",
    },
    # ── 2028 Global Intelligence Crisis Keywords ──────────────────────────────
    {
        "keywords": [
            "ghost GDP", "intelligence crisis", "white collar job loss",
            "AI mass unemployment", "AI displacement spiral", "2028 scenario"
        ],
        "stocks_to_avoid": ["V", "MA", "UBER", "NOW", "CRM", "SPY", "QQQ", "TQQQ"],
        "stocks_to_watch": ["NVDA", "GLD", "IAU", "SLV"],
        "severity": "CRITICAL",
        "reason": "2028 GIC scenario: AI mass unemployment → consumer spending collapse → S&P 500 target 3,500",
        "sector": "Macro / Systemic",
    },
]


def _parse_rss_feed(url: str, hours_back: int = 24) -> List[Dict]:
    """Fetch and parse an RSS/Atom feed, returning recent posts."""
    posts = []
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        ns = {}

        # Detect Atom vs RSS
        if "feed" in root.tag.lower() or root.tag.startswith("{http://www.w3.org/2005/Atom}"):
            # Atom feed
            atom_ns = "http://www.w3.org/2005/Atom"
            for entry in root.findall(f"{{{atom_ns}}}entry"):
                title_el = entry.find(f"{{{atom_ns}}}title")
                link_el = entry.find(f"{{{atom_ns}}}link")
                updated_el = entry.find(f"{{{atom_ns}}}updated") or entry.find(f"{{{atom_ns}}}published")
                summary_el = entry.find(f"{{{atom_ns}}}summary") or entry.find(f"{{{atom_ns}}}content")

                title = title_el.text if title_el is not None else ""
                link = link_el.get("href", "") if link_el is not None else ""
                summary = summary_el.text if summary_el is not None else ""
                pub_str = updated_el.text if updated_el is not None else ""
                pub_time = _parse_date(pub_str)

                if pub_time and pub_time >= cutoff:
                    posts.append({"title": title, "link": link, "summary": (summary or "")[:300], "published": pub_time})
        else:
            # RSS 2.0 feed
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else root.findall(".//item")
            for item in items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "").strip()
                pub_str = item.findtext("pubDate") or item.findtext("dc:date") or ""
                pub_time = _parse_date(pub_str)

                if pub_time and pub_time >= cutoff:
                    posts.append({"title": title, "link": link, "summary": desc[:300], "published": pub_time})

    except ET.ParseError as e:
        logger.debug(f"[BlogMonitor] XML parse error for {url}: {e}")
    except Exception as e:
        logger.debug(f"[BlogMonitor] Feed fetch error for {url}: {e}")
    return posts


def _parse_date(date_str: str) -> Optional[datetime]:
    """Try multiple date formats used by blog RSS feeds."""
    if not date_str:
        return None
    date_str = date_str.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822: Tue, 25 Feb 2025 10:00:00 +0000
        "%a, %d %b %Y %H:%M:%S GMT",  # RFC 2822 with GMT literal
        "%Y-%m-%dT%H:%M:%SZ",         # ISO 8601 UTC
        "%Y-%m-%dT%H:%M:%S+00:00",    # ISO 8601 with timezone
        "%Y-%m-%dT%H:%M:%S%z",        # ISO 8601 with tz offset
        "%Y-%m-%d",                    # Date only
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:len(fmt) + 5], fmt)
            # Make timezone-naive for comparison
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _match_impact(title: str, summary: str) -> List[Dict]:
    """
    Check a blog post title+summary against the BLOG_IMPACT_MAP.
    Returns list of impact entries that matched.
    """
    text = (title + " " + summary).lower()
    matched = []
    for impact in BLOG_IMPACT_MAP:
        hits = [kw for kw in impact["keywords"] if kw.lower() in text]
        if hits:
            matched.append({
                **impact,
                "matched_keywords": hits,
            })
    return matched


def scan_all_blogs(hours_back: int = 24) -> List[Dict]:
    """
    Scan all configured blog RSS feeds for recent posts.
    Returns list of alerts: {source, title, link, published, impact_entries}.
    """
    alerts = []
    for feed_id, feed_config in BLOG_FEEDS.items():
        posts = _parse_rss_feed(feed_config["rss_url"], hours_back)
        if not posts:
            logger.debug(f"[BlogMonitor] No recent posts from {feed_config['name']} (or feed unavailable)")
            continue

        for post in posts:
            impacts = _match_impact(post["title"], post.get("summary", ""))
            if impacts:
                alert = {
                    "source_id": feed_id,
                    "source_name": feed_config["name"],
                    "company_type": feed_config["company_type"],
                    "title": post["title"],
                    "link": post.get("link", ""),
                    "published": post["published"].isoformat() if post.get("published") else "",
                    "impacts": impacts,
                    "max_severity": _max_severity(impacts),
                }
                alerts.append(alert)
                logger.warning(
                    f"[BlogMonitor] 🚨 {feed_config['name']}: \"{post['title']}\"\n"
                    f"   Impact: {[i['sector'] for i in impacts]} | "
                    f"Avoid: {[s for i in impacts for s in i['stocks_to_avoid']]} | "
                    f"Watch: {[s for i in impacts for s in i['stocks_to_watch']]}"
                )

    return alerts


def _max_severity(impacts: List[Dict]) -> str:
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    severities = [i.get("severity", "LOW") for i in impacts]
    return min(severities, key=lambda s: order.get(s, 99))


def build_blog_alert_context(alerts: List[Dict], target_symbol: str = "") -> str:
    """
    Build AI prompt context for detected blog alerts.
    If target_symbol is given, only include alerts relevant to that symbol.
    """
    if not alerts:
        return ""

    relevant = []
    for alert in alerts:
        all_affected = set()
        for imp in alert["impacts"]:
            all_affected.update(imp["stocks_to_avoid"])
            all_affected.update(imp["stocks_to_watch"])
        if not target_symbol or target_symbol in all_affected:
            relevant.append(alert)

    if not relevant:
        return ""

    lines = [f"### 📰 OFFICIAL BLOG INTELLIGENCE ALERTS"]
    lines.append("(Source: First-party company blogs — highest confidence signals)\n")

    for alert in relevant:
        severity_emoji = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "📌", "LOW": "ℹ️"}.get(alert["max_severity"], "📌")
        lines.append(f"{severity_emoji} [{alert['max_severity']}] {alert['source_name']}: \"{alert['title']}\"")
        lines.append(f"  Published: {alert['published'][:10]} | Link: {alert['link']}")
        for imp in alert["impacts"]:
            lines.append(f"  → Sector: {imp['sector']}")
            lines.append(f"  → Reason: {imp['reason']}")
            if imp["stocks_to_avoid"]:
                lines.append(f"  → SELL/AVOID: {', '.join(imp['stocks_to_avoid'])}")
            if imp["stocks_to_watch"]:
                lines.append(f"  → CONSIDER: {', '.join(imp['stocks_to_watch'])}")
            lines.append(f"  → Keywords matched: {', '.join(imp['matched_keywords'])}")

    lines.append(
        "\n  ⚠️ INSTRUCTION: These are OFFICIAL blog posts from major AI companies — "
        "highest confidence competitive signals. Act immediately: SELL affected stocks, "
        "consider BUY on beneficiaries."
    )
    return "\n".join(lines)


def get_affected_symbols(alerts: List[Dict]) -> Dict[str, List[str]]:
    """
    Extract all stocks affected by blog alerts.
    Returns {"sell": [...], "watch": [...]}
    """
    to_sell = set()
    to_watch = set()
    for alert in alerts:
        for imp in alert["impacts"]:
            to_sell.update(imp["stocks_to_avoid"])
            to_watch.update(imp["stocks_to_watch"])
    return {"sell": list(to_sell), "watch": list(to_watch)}
