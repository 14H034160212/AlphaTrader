"""
LLM-driven catalyst extractor.

Replaces (or augments) static CATALYST_MAP keyword matching with per-headline
LLM classification. Solves the brittle-keyword problem: novel catalyst types
(US gov stake in INTC, sovereign AI deals, takeover bids, etc.) are detected
without us hand-adding keywords for every new event-class the market invents.

Output is plug-compatible with `news_intelligence.detect_catalysts_for_symbol()`
— same list-of-dicts shape so downstream code (build_catalyst_context,
deepseek_ai prompt construction) works unchanged.

Approach
========
For each symbol:
  1. Pull fresh headlines via news_intelligence.fetch_news_with_fallback().
  2. Filter out headlines we've already classified (cache keyed by
     (symbol, url-or-title-hash)).
  3. Batch 5 fresh headlines into a single LLM call asking JSON:
       [{is_catalyst, direction, severity, thesis, confidence}, ...]
  4. Convert to the same dict shape detect_catalysts_for_symbol returns.
  5. Cache + return.

Created 2026-05-23 after SerenityAlphaTrader missed the US-gov-takes-Intel-stake
catalyst because "government stake" wasn't in any static keyword list.
"""
from __future__ import annotations
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import news_intelligence as ni

logger = logging.getLogger(__name__)


# ── In-memory cache: (symbol, headline_hash) → classified_dict ─────────────
# Expires after 6h so news that gets re-fetched doesn't stay frozen forever.
_LLM_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 6 * 3600


def _headline_hash(item: dict) -> str:
    """Stable ID for a news item — prefer URL, fallback to title hash."""
    key = item.get("url") or item.get("link") or item.get("title", "")
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _cached(symbol: str, h: str) -> Optional[dict]:
    rec = _LLM_CACHE.get((symbol, h))
    if rec is None:
        return None
    ts, payload = rec
    if time.time() - ts > _CACHE_TTL_SECONDS:
        _LLM_CACHE.pop((symbol, h), None)
        return None
    return payload


def _cache_put(symbol: str, h: str, payload: dict) -> None:
    _LLM_CACHE[(symbol, h)] = (time.time(), payload)


# ── LLM classification ───────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a stock-market catalyst classifier. For each news headline + symbol pair, "
    "decide if the news is a real catalyst that would move the stock price meaningfully "
    "in the next 1-5 trading days. Respond ONLY with valid JSON array. "
    "/no_think"
)


def _build_user_prompt(symbol: str, headlines: list[dict]) -> str:
    items = []
    for i, h in enumerate(headlines):
        title = (h.get("title") or "").strip()
        pub = h.get("publisher", "")
        when = h.get("time", "")
        items.append(f'  {i}. [{pub}] {title}  (published: {when})')
    items_str = "\n".join(items)
    return (
        f"Symbol: {symbol}\n\n"
        f"Headlines to classify:\n{items_str}\n\n"
        f"For EACH headline above, return a JSON object with these fields:\n"
        f"  index: int (matches the headline number above)\n"
        f'  is_catalyst: bool — true ONLY if this news materially affects {symbol} price 1-5 days\n'
        f"  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL'\n"
        f"  severity: 'MILD' | 'MEDIUM' | 'STRONG'\n"
        f"  thesis: short one-sentence rationale (≤25 words)\n"
        f"  confidence: float 0.0-1.0\n\n"
        f"Rules:\n"
        f"- Generic market-roundup articles that just mention {symbol} = is_catalyst=false\n"
        f"- Macro/sector news that DIRECTLY benefits/hurts {symbol} = is_catalyst=true\n"
        f"- Earnings, contract wins, regulatory actions, government stakes, M&A = is_catalyst=true\n"
        f"- Unrelated news (UFO sightings, other companies' news) = is_catalyst=false\n\n"
        f'Output a JSON array: [{{"index":0,"is_catalyst":true,...}},{{"index":1,...}}]'
    )


def _llm_classify_batch(
    symbol: str,
    headlines: list[dict],
    llm_call_fn=None,
) -> list[dict]:
    """
    Send N headlines to LLM, get back classifications. Returns list of
    {index, is_catalyst, direction, severity, thesis, confidence} dicts.
    Falls back to empty list on any failure (caller treats as no catalyst).
    """
    if not headlines:
        return []
    if llm_call_fn is None:
        import deepseek_ai
        llm_call_fn = deepseek_ai._call_ollama
    prompt = _build_user_prompt(symbol, headlines)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = llm_call_fn(messages, temperature=0.1)
    except Exception as e:
        logger.warning(f"[LLM Catalyst] LLM call failed for {symbol}: {e}")
        return []

    # Parse JSON — be tolerant of preamble/postamble text
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        logger.debug(f"[LLM Catalyst] {symbol}: no JSON array in response — head: {raw[:200]}")
        return []
    try:
        parsed = json.loads(m.group(0))
        if not isinstance(parsed, list):
            return []
        return parsed
    except json.JSONDecodeError as e:
        logger.debug(f"[LLM Catalyst] {symbol}: JSON parse error: {e}")
        return []


# ── Output conversion to existing catalyst-dict shape ───────────────────────


def _to_catalyst_dict(
    symbol: str,
    headline: dict,
    classification: dict,
) -> dict:
    """Convert LLM JSON to the dict shape news_intelligence emits."""
    sev = (classification.get("severity") or "MILD").upper()
    direction = (classification.get("direction") or "NEUTRAL").upper()
    return {
        "target_symbol": symbol,
        "news_title": headline.get("title", ""),
        "news_origin": symbol,
        "publisher": headline.get("publisher", ""),
        "time": headline.get("time", ""),
        "matched_keywords": [],   # LLM doesn't use keyword matching
        "upside_thesis": classification.get("thesis", ""),
        "strength": {"MILD": 1, "MEDIUM": 2, "STRONG": 3}.get(sev, 1),
        "catalyst_level": sev,
        "source": "llm:" + (headline.get("source") or "rss"),
        # ── LLM-only fields (extras downstream can use) ──
        "llm_direction": direction,
        "llm_confidence": float(classification.get("confidence", 0.5)),
        "llm_is_catalyst": bool(classification.get("is_catalyst", False)),
    }


# ── Public entry: per-symbol catalyst extraction via LLM ────────────────────


def extract_catalysts_for_symbol(
    symbol: str,
    hours_back: int = 24,
    max_headlines_per_call: int = 5,
    llm_call_fn=None,
) -> list[dict]:
    """
    Pull recent headlines for SYMBOL, classify each via LLM, return catalysts.

    Output is plug-compatible with news_intelligence.detect_catalysts_for_symbol():
    list of dicts with the same keys so downstream context-building / prompt
    construction works without changes.
    """
    try:
        news = ni.fetch_news_with_fallback(symbol, hours_back)
    except Exception as e:
        logger.warning(f"[LLM Catalyst] news fetch failed for {symbol}: {e}")
        return []

    fresh = []
    cached_hits = []
    for item in news or []:
        if not item.get("title"):
            continue
        h = _headline_hash(item)
        cached = _cached(symbol, h)
        if cached:
            if cached.get("is_catalyst"):
                cached_hits.append(_to_catalyst_dict(symbol, item, cached))
            continue
        fresh.append((h, item))

    if not fresh:
        return cached_hits

    # Batch in groups of N
    new_results = []
    for i in range(0, len(fresh), max_headlines_per_call):
        batch = fresh[i:i + max_headlines_per_call]
        headlines_only = [it for _, it in batch]
        classifications = _llm_classify_batch(symbol, headlines_only, llm_call_fn)
        # Index-aligned merge
        cls_by_idx = {int(c.get("index", -1)): c for c in classifications}
        for idx, (h, item) in enumerate(batch):
            c = cls_by_idx.get(idx)
            if not c:
                # LLM didn't classify this one — cache as non-catalyst to avoid re-call
                _cache_put(symbol, h, {"is_catalyst": False})
                continue
            _cache_put(symbol, h, c)
            if c.get("is_catalyst"):
                new_results.append(_to_catalyst_dict(symbol, item, c))

    return cached_hits + new_results


# ── Cache stats / admin ─────────────────────────────────────────────────────


def extract_sector_catalysts(
    focus_symbols: list[str],
    hours_back: int = 12,
    max_headlines: int = 40,
    llm_call_fn=None,
) -> dict[str, list[dict]]:
    """
    SECTOR-WIDE catalyst capture (user 2026-05-27: "capture all GPU/chip market
    dynamics"). Pulls broad semiconductor / AI / tech news from TECH_RSS_SOURCES
    (Tom's Hardware, EE Times, Semiconductor Engineering, IEEE, etc.) — news that
    isn't tied to a single ticker — and uses the LLM to map each story to which
    focus-theme symbols it affects + direction.

    Returns {symbol: [catalyst_dict, ...]} for affected focus symbols.
    Complements the per-ticker extract_catalysts_for_symbol().
    """
    try:
        tech_news = ni.fetch_tech_news(hours_back=hours_back)
    except Exception as e:
        logger.warning(f"[LLM SectorCatalyst] tech news fetch failed: {e}")
        return {}
    if not tech_news:
        return {}

    # Dedup vs cache (reuse same cache keyed by ('__sector__', hash))
    fresh = []
    for item in tech_news[:max_headlines]:
        if not item.get("title"):
            continue
        h = _headline_hash(item)
        if _cached("__sector__", h) is not None:
            continue
        fresh.append((h, item))
    if not fresh:
        return {}

    if llm_call_fn is None:
        import deepseek_ai
        llm_call_fn = deepseek_ai._call_ollama

    focus_str = ", ".join(focus_symbols[:60])
    results: dict[str, list[dict]] = {}

    # Batch headlines, ask LLM to map each to affected focus tickers
    for i in range(0, len(fresh), 6):
        batch = fresh[i:i+6]
        lines = [f"  {j}. [{it.get('publisher','')}] {it.get('title','')}"
                 for j, (_, it) in enumerate(batch)]
        prompt = (
            f"Focus stock universe (semiconductors / GPU supply chain / AI / robotics):\n{focus_str}\n\n"
            f"Sector news headlines:\n" + "\n".join(lines) + "\n\n"
            f"For EACH headline, identify which focus stocks (if any) it materially "
            f"affects in the next 1-5 trading days. Return JSON array, one object per "
            f"AFFECTED (headline, symbol) pair:\n"
            f'  [{{"index":0,"symbol":"NVDA","direction":"BULLISH","severity":"STRONG",'
            f'"thesis":"...","confidence":0.8}}, ...]\n'
            f"Rules: only include real, material impacts. A headline can affect 0, 1, "
            f"or several focus stocks. Generic news = skip. Output [] if nothing material."
        )
        messages = [
            {"role":"system","content":"You map sector news to affected stocks. Output JSON array only. /no_think"},
            {"role":"user","content": prompt},
        ]
        try:
            raw = llm_call_fn(messages, temperature=0.1)
        except Exception as e:
            logger.debug(f"[LLM SectorCatalyst] batch {i} failed: {e}")
            continue
        # Mark all batch headlines cached (so we don't reprocess)
        for h, _ in batch:
            _cache_put("__sector__", h, {"is_catalyst": False})
        if not raw:
            continue
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            continue
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        for c in parsed:
            idx = int(c.get("index", -1))
            sym = (c.get("symbol") or "").upper()
            if sym not in focus_symbols or not (0 <= idx < len(batch)):
                continue
            _, item = batch[idx]
            cat = _to_catalyst_dict(sym, item, {
                "severity": c.get("severity","MILD"),
                "direction": c.get("direction","NEUTRAL"),
                "thesis": c.get("thesis",""),
                "confidence": c.get("confidence",0.5),
                "is_catalyst": True,
            })
            cat["source"] = "llm:sector"
            results.setdefault(sym, []).append(cat)

    if results:
        logger.info(f"[LLM SectorCatalyst] mapped sector news → {len(results)} focus stocks: "
                    f"{list(results.keys())[:10]}")
    return results


def cache_stats() -> dict:
    """Diagnostics for monitoring (size, hit-rate, etc.)."""
    now = time.time()
    total = len(_LLM_CACHE)
    expired = sum(1 for ts, _ in _LLM_CACHE.values() if now - ts > _CACHE_TTL_SECONDS)
    catalysts = sum(1 for _, p in _LLM_CACHE.values() if p.get("is_catalyst"))
    return {
        "total_classifications": total,
        "expired": expired,
        "catalyst_count": catalysts,
        "cache_ttl_hours": _CACHE_TTL_SECONDS // 3600,
    }


def cache_clear() -> int:
    """Drop all cache entries. Returns count removed."""
    n = len(_LLM_CACHE)
    _LLM_CACHE.clear()
    return n
