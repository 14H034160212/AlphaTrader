"""
Layoff Event Framework
Quantifies market reaction after layoff announcements.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional
import math
import logging
import re

import yfinance as yf

logger = logging.getLogger(__name__)

EVENT_WINDOWS = [0, 1, 3, 5, 10, 20]
LAYOFF_KEYWORDS = [
    "layoff", "layoffs", "laid off", "job cuts", "cut jobs", "cuts jobs",
    "restructuring", "workforce reduction", "cost reduction", "cost cuts",
    "headcount reduction", "streamlining", "right-sizing",
]


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def _fetch_close_series(symbol: str, announcement_date: str, lookahead_days: int) -> List[Dict]:
    event_dt = _parse_date(announcement_date)
    start = (event_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (event_dt + timedelta(days=max(lookahead_days, 20) + 20)).strftime("%Y-%m-%d")
    hist = yf.Ticker(symbol).history(start=start, end=end, interval="1d")
    if hist.empty:
        return []

    rows = []
    for idx, row in hist.iterrows():
        rows.append({
            "date": idx.date().isoformat(),
            "close": float(row["Close"]),
        })
    return rows


def _find_event_index(prices: List[Dict], announcement_date: str) -> Optional[int]:
    if not prices:
        return None
    for i, row in enumerate(prices):
        if row["date"] >= announcement_date:
            return i
    return None


def _event_window_returns(prices: List[Dict], event_idx: int, windows: List[int]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    if event_idx <= 0 or event_idx >= len(prices):
        return {f"day_{w}": None for w in windows}

    base = prices[event_idx - 1]["close"]
    for w in windows:
        idx = event_idx + w
        key = f"day_{w}"
        if idx >= len(prices) or base == 0:
            out[key] = None
        else:
            out[key] = round((prices[idx]["close"] / base - 1) * 100, 3)
    return out


def _path_returns(prices: List[Dict], event_idx: int, lookahead_days: int) -> List[Dict]:
    if event_idx <= 0 or event_idx >= len(prices):
        return []
    base = prices[event_idx - 1]["close"]
    if base == 0:
        return []
    path = []
    for d in range(0, lookahead_days + 1):
        idx = event_idx + d
        if idx >= len(prices):
            break
        rel = (prices[idx]["close"] / base - 1) * 100
        path.append({
            "day": d,
            "date": prices[idx]["date"],
            "return_pct": round(rel, 3),
            "close": round(prices[idx]["close"], 4),
        })
    return path


def _sustained_reaction_days(prices: List[Dict], event_idx: int, lookahead_days: int, day1_ret: Optional[float]) -> int:
    if event_idx <= 0 or event_idx >= len(prices) or day1_ret is None or day1_ret == 0:
        return 0

    base = prices[event_idx - 1]["close"]
    direction = 1 if day1_ret > 0 else -1
    sustained = 0

    for i in range(0, lookahead_days + 1):
        idx = event_idx + i
        if idx >= len(prices):
            break
        rel = prices[idx]["close"] / base - 1
        if rel == 0:
            break
        if (rel > 0 and direction > 0) or (rel < 0 and direction < 0):
            sustained += 1
        else:
            break
    return sustained


def _normalize_guidance(guidance_change: Optional[str]) -> int:
    if not guidance_change:
        return 0
    raw = guidance_change.strip().lower()
    if raw in ("up", "raise", "raised", "positive", "better"):
        return 1
    if raw in ("down", "cut", "lower", "negative", "worse"):
        return -1
    return 0


def _event_strength_score(
    layoff_percentage: Optional[float],
    layoff_employees: Optional[int],
    guidance_change: Optional[str],
    day1_ret: Optional[float],
    day5_ret: Optional[float],
) -> float:
    pct = max(0.0, min(layoff_percentage or 0.0, 30.0))
    pct_component = min(35.0, pct * 1.75)

    headcount = max(0, layoff_employees or 0)
    headcount_component = min(20.0, math.log10(headcount + 1) * 5.0)

    guidance = _normalize_guidance(guidance_change)
    guidance_component = 20.0 if guidance > 0 else -20.0 if guidance < 0 else 0.0

    reaction_component = 0.0
    if day1_ret is not None:
        reaction_component += max(-15.0, min(15.0, day1_ret * 2.0))
    if day5_ret is not None:
        reaction_component += max(-10.0, min(10.0, day5_ret))

    score = pct_component + headcount_component + guidance_component + reaction_component + 25.0
    return round(max(0.0, min(score, 100.0)), 2)


def analyze_layoff_event(
    symbol: str,
    announcement_date: str,
    layoff_percentage: Optional[float] = None,
    layoff_employees: Optional[int] = None,
    guidance_change: Optional[str] = None,
    benchmark_symbol: str = "SPY",
    lookahead_days: int = 20,
) -> Dict:
    prices = _fetch_close_series(symbol, announcement_date, lookahead_days)
    event_idx = _find_event_index(prices, announcement_date)
    if event_idx is None or event_idx == 0:
        return {
            "symbol": symbol,
            "announcement_date": announcement_date,
            "error": "Insufficient price history around announcement date",
        }

    windows = [w for w in EVENT_WINDOWS if w <= lookahead_days]
    returns = _event_window_returns(prices, event_idx, windows)
    path = _path_returns(prices, event_idx, lookahead_days)
    day1_ret = returns.get("day_1")
    day5_ret = returns.get("day_5")
    sustain_days = _sustained_reaction_days(prices, event_idx, lookahead_days, day1_ret)

    benchmark_returns = None
    if benchmark_symbol:
        bench_prices = _fetch_close_series(benchmark_symbol, announcement_date, lookahead_days)
        bench_idx = _find_event_index(bench_prices, announcement_date)
        if bench_idx is not None and bench_idx > 0:
            benchmark_returns = _event_window_returns(bench_prices, bench_idx, windows)

    abnormal_returns = {}
    if benchmark_returns:
        for k, v in returns.items():
            b = benchmark_returns.get(k)
            abnormal_returns[k] = None if v is None or b is None else round(v - b, 3)

    score = _event_strength_score(
        layoff_percentage=layoff_percentage,
        layoff_employees=layoff_employees,
        guidance_change=guidance_change,
        day1_ret=day1_ret,
        day5_ret=day5_ret,
    )

    return {
        "symbol": symbol,
        "announcement_date": announcement_date,
        "layoff_percentage": layoff_percentage,
        "layoff_employees": layoff_employees,
        "guidance_change": guidance_change,
        "reaction_duration_days": sustain_days,
        "event_window_returns_pct": returns,
        "path_returns_pct": path,
        "benchmark_symbol": benchmark_symbol if benchmark_returns else None,
        "benchmark_window_returns_pct": benchmark_returns,
        "abnormal_returns_pct": abnormal_returns if abnormal_returns else None,
        "event_strength_score": score,
    }


def analyze_layoff_events(events: List[Dict], benchmark_symbol: str = "SPY", lookahead_days: int = 20) -> Dict:
    results = []
    for event in events:
        try:
            results.append(
                analyze_layoff_event(
                    symbol=event["symbol"],
                    announcement_date=event["announcement_date"],
                    layoff_percentage=event.get("layoff_percentage"),
                    layoff_employees=event.get("layoff_employees"),
                    guidance_change=event.get("guidance_change"),
                    benchmark_symbol=benchmark_symbol,
                    lookahead_days=lookahead_days,
                )
            )
        except Exception as e:
            logger.error(f"[LayoffFramework] Failed to analyze event {event}: {e}")
            results.append({
                "symbol": event.get("symbol"),
                "announcement_date": event.get("announcement_date"),
                "error": str(e),
            })

    valid = [r for r in results if "error" not in r]
    avg_day1 = round(sum((r["event_window_returns_pct"].get("day_1") or 0) for r in valid) / len(valid), 3) if valid else None
    avg_sustain = round(sum(r["reaction_duration_days"] for r in valid) / len(valid), 2) if valid else None
    avg_score = round(sum(r["event_strength_score"] for r in valid) / len(valid), 2) if valid else None

    return {
        "count_total": len(results),
        "count_valid": len(valid),
        "avg_day1_return_pct": avg_day1,
        "avg_reaction_duration_days": avg_sustain,
        "avg_event_strength_score": avg_score,
        "results": results,
    }


def _extract_layoff_metadata(title: str) -> Dict:
    txt = title.lower()

    pct = None
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
    if pct_match:
        try:
            pct = float(pct_match.group(1))
        except Exception:
            pct = None

    employees = None
    emp_match = re.search(r"(\d[\d,]{2,})\s+(jobs|employees|workers|staff)", txt)
    if emp_match:
        try:
            employees = int(emp_match.group(1).replace(",", ""))
        except Exception:
            employees = None

    guidance = None
    if any(k in txt for k in ("raises guidance", "guidance raised", "guidance up", "outlook raised")):
        guidance = "up"
    elif any(k in txt for k in ("cuts guidance", "guidance cut", "guidance lowered", "outlook lowered")):
        guidance = "down"

    return {
        "layoff_percentage": pct,
        "layoff_employees": employees,
        "guidance_change": guidance,
    }


def discover_layoff_candidates(symbols: List[str], hours_back: int = 168, max_items: int = 50) -> Dict:
    """
    Semi-automatic discovery of layoff/restructuring events from recent news headlines.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    candidates = []
    seen = set()

    for symbol in symbols:
        try:
            news = yf.Ticker(symbol).news or []
        except Exception as e:
            logger.debug(f"[LayoffFramework] No news for {symbol}: {e}")
            continue

        for item in news:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            title_l = title.lower()
            if not any(k in title_l for k in LAYOFF_KEYWORDS):
                continue

            pub_ts = item.get("providerPublishTime", 0) or 0
            try:
                pub_dt = datetime.utcfromtimestamp(pub_ts)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue

            link = item.get("link") or ""
            unique_key = f"{symbol}|{title}|{pub_dt.date().isoformat()}"
            if unique_key in seen:
                continue
            seen.add(unique_key)

            meta = _extract_layoff_metadata(title)
            candidates.append({
                "symbol": symbol,
                "announcement_date": pub_dt.date().isoformat(),
                "headline_time": pub_dt.isoformat(),
                "headline": title,
                "publisher": item.get("publisher", ""),
                "link": link,
                "matched_keywords": [k for k in LAYOFF_KEYWORDS if k in title_l],
                "layoff_percentage": meta["layoff_percentage"],
                "layoff_employees": meta["layoff_employees"],
                "guidance_change": meta["guidance_change"],
            })

    candidates.sort(key=lambda x: x["headline_time"], reverse=True)
    candidates = candidates[:max_items]
    return {
        "hours_back": hours_back,
        "count": len(candidates),
        "candidates": candidates,
    }
