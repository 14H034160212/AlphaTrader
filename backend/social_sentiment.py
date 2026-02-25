"""
Social Sentiment Analysis - Free APIs
Aggregates retail investor sentiment from StockTwits and Reddit
to detect crowd positioning and momentum shifts.

StockTwits: Free public API, no auth required.
Reddit: Free public JSON API, no auth required (User-Agent needed).
"""
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "AlphaTrader/1.0 (stock research tool)"}
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REDDIT_SEARCH_URL = "https://www.reddit.com/r/{sub}/search.json"

REDDIT_SUBS = ["wallstreetbets", "stocks", "investing"]

# Keywords that indicate strong bearish/bullish sentiment
BEARISH_WORDS = [
    "crash", "dump", "short", "puts", "bear", "overvalued", "bubble", "sell",
    "baghold", "rug pull", "scam", "fraud", "bankruptcy", "drop", "tank",
    "collapse", "recession", "crisis", "2028", "disaster"
]
BULLISH_WORDS = [
    "moon", "buy", "calls", "bull", "undervalued", "dip", "accumulate",
    "long", "hold", "breakout", "rally", "earnings beat", "squeeze", "yolo"
]


def get_stocktwits_sentiment(symbol: str) -> dict:
    """
    Fetch latest StockTwits messages for a symbol.
    Returns bullish/bearish counts and sentiment score.
    """
    try:
        url = STOCKTWITS_URL.format(symbol=symbol)
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            messages = data.get("messages", [])
        elif resp.status_code == 404:
            return {"symbol": symbol, "source": "stocktwits", "error": "symbol not found"}
        else:
            return {"symbol": symbol, "source": "stocktwits", "error": f"HTTP {resp.status_code}"}

        bullish = 0
        bearish = 0
        neutral = 0
        sample_msgs = []

        for msg in messages[:30]:
            sentiment = msg.get("entities", {}).get("sentiment")
            if sentiment:
                label = sentiment.get("basic", "").lower()
                if label == "bullish":
                    bullish += 1
                elif label == "bearish":
                    bearish += 1
                else:
                    neutral += 1
            else:
                # Fallback: keyword scan
                body = msg.get("body", "").lower()
                b_score = sum(1 for w in BULLISH_WORDS if w in body)
                bear_score = sum(1 for w in BEARISH_WORDS if w in body)
                if b_score > bear_score:
                    bullish += 1
                elif bear_score > b_score:
                    bearish += 1
                else:
                    neutral += 1

            if len(sample_msgs) < 3:
                sample_msgs.append(msg.get("body", "")[:120])

        total = bullish + bearish + neutral or 1
        score = (bullish - bearish) / total  # Range: -1.0 (extreme bearish) to +1.0 (extreme bullish)

        return {
            "symbol": symbol,
            "source": "stocktwits",
            "total_messages": total,
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "sentiment_score": round(score, 3),
            "sentiment_label": "BULLISH" if score > 0.2 else ("BEARISH" if score < -0.2 else "NEUTRAL"),
            "sample_messages": sample_msgs,
        }
    except Exception as e:
        logger.debug(f"[Sentiment] StockTwits error for {symbol}: {e}")
        return {"symbol": symbol, "source": "stocktwits", "error": str(e)}


def get_reddit_sentiment(symbol: str, hours_back: int = 24) -> dict:
    """
    Search Reddit for recent posts mentioning the symbol.
    Aggregates sentiment from r/wallstreetbets, r/stocks, r/investing.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    all_posts = []
    bullish = 0
    bearish = 0
    neutral = 0

    for sub in REDDIT_SUBS:
        try:
            resp = requests.get(
                REDDIT_SEARCH_URL.format(sub=sub),
                params={"q": symbol, "sort": "new", "limit": 15, "t": "day", "restrict_sr": "on"},
                headers=HEADERS,
                timeout=10
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            posts = data.get("data", {}).get("children", [])

            for post in posts:
                p = post.get("data", {})
                created = datetime.utcfromtimestamp(p.get("created_utc", 0))
                if created < cutoff:
                    continue

                title = p.get("title", "").lower()
                text = (p.get("selftext", "") or "").lower()
                content = title + " " + text

                b_score = sum(1 for w in BULLISH_WORDS if w in content)
                bear_score = sum(1 for w in BEARISH_WORDS if w in content)
                score_val = p.get("score", 0)

                # Weighted by upvotes
                weight = max(1, min(score_val, 100))
                if b_score > bear_score:
                    bullish += weight
                    sentiment = "BULLISH"
                elif bear_score > b_score:
                    bearish += weight
                    sentiment = "BEARISH"
                else:
                    neutral += weight
                    sentiment = "NEUTRAL"

                all_posts.append({
                    "subreddit": sub,
                    "title": p.get("title", "")[:100],
                    "score": score_val,
                    "sentiment": sentiment,
                })

        except Exception as e:
            logger.debug(f"[Sentiment] Reddit r/{sub} error for {symbol}: {e}")

    total = bullish + bearish + neutral or 1
    score = (bullish - bearish) / total
    top_posts = sorted(all_posts, key=lambda x: x["score"], reverse=True)[:3]

    return {
        "symbol": symbol,
        "source": "reddit",
        "subreddits": REDDIT_SUBS,
        "total_posts": len(all_posts),
        "bullish_weight": bullish,
        "bearish_weight": bearish,
        "sentiment_score": round(score, 3),
        "sentiment_label": "BULLISH" if score > 0.15 else ("BEARISH" if score < -0.15 else "NEUTRAL"),
        "top_posts": top_posts,
    }


def get_combined_sentiment(symbol: str) -> dict:
    """Combine StockTwits + Reddit into a single sentiment summary."""
    st = get_stocktwits_sentiment(symbol)
    rd = get_reddit_sentiment(symbol)

    st_score = st.get("sentiment_score", 0) if "error" not in st else 0
    rd_score = rd.get("sentiment_score", 0) if "error" not in rd else 0

    # Weight StockTwits 60% (more stock-focused), Reddit 40%
    combined_score = st_score * 0.6 + rd_score * 0.4

    if combined_score > 0.2:
        label = "BULLISH"
    elif combined_score < -0.2:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {
        "symbol": symbol,
        "combined_score": round(combined_score, 3),
        "combined_label": label,
        "stocktwits": st,
        "reddit": rd,
    }


def build_sentiment_context(symbol: str) -> str:
    """
    Build a context string for the AI about current social sentiment.
    Injected into the AI analysis prompt so it knows what retail is thinking.
    """
    try:
        data = get_combined_sentiment(symbol)
    except Exception as e:
        return ""

    score = data.get("combined_score", 0)
    label = data.get("combined_label", "NEUTRAL")
    st = data.get("stocktwits", {})
    rd = data.get("reddit", {})

    lines = [f"### 📱 Social Sentiment for {symbol}"]
    lines.append(
        f"Combined Sentiment: **{label}** (score: {score:+.2f})\n"
        f"  → StockTwits: {st.get('bullish', 'N/A')} bullish / {st.get('bearish', 'N/A')} bearish "
        f"({st.get('total_messages', 0)} messages) — {st.get('sentiment_label', 'N/A')}\n"
        f"  → Reddit: {rd.get('total_posts', 0)} posts across r/wallstreetbets, r/stocks, r/investing "
        f"— {rd.get('sentiment_label', 'N/A')}"
    )

    # Sample messages from StockTwits
    samples = st.get("sample_messages", [])
    if samples:
        lines.append("  StockTwits samples:")
        for s in samples:
            lines.append(f'    • "{s}"')

    # Top Reddit posts
    top_posts = rd.get("top_posts", [])
    if top_posts:
        lines.append("  Top Reddit mentions:")
        for p in top_posts:
            lines.append(f'    • [{p["subreddit"]}] {p["title"]} (↑{p["score"]} | {p["sentiment"]})')

    # Instruction for AI
    if label == "BEARISH" and score < -0.3:
        lines.append(
            f"\n  ⚠️ INSTRUCTION: Extreme retail bearishness detected. This may indicate "
            f"capitulation (contrarian BUY signal) OR momentum continuation. "
            f"Check if institutional flow confirms direction before acting."
        )
    elif label == "BULLISH" and score > 0.4:
        lines.append(
            f"\n  ⚠️ INSTRUCTION: Extreme retail bullishness detected (potential crowded trade). "
            f"Check for overvaluation and consider SELL if institutions are distributing."
        )
    else:
        lines.append(
            f"\n  INFO: Moderate retail sentiment — use as secondary signal only."
        )

    return "\n".join(lines)


def scan_sentiment_alerts(watchlist: list) -> dict:
    """
    Quick scan of the entire watchlist for extreme sentiment.
    Returns dict of symbol -> sentiment data for symbols with strong signals.
    """
    alerts = {}
    for symbol in watchlist:
        try:
            st = get_stocktwits_sentiment(symbol)
            if "error" in st:
                continue
            score = st.get("sentiment_score", 0)
            # Only flag extreme sentiment
            if abs(score) >= 0.35 and st.get("total_messages", 0) >= 5:
                alerts[symbol] = st
                label = st.get("sentiment_label", "NEUTRAL")
                logger.info(
                    f"[SocialScan] {symbol}: {label} ({score:+.2f}) "
                    f"— {st['bullish']}↑ {st['bearish']}↓ on StockTwits"
                )
        except Exception as e:
            logger.debug(f"[SocialScan] Error for {symbol}: {e}")
    return alerts
