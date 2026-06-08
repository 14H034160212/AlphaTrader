"""DeepSeek-R1 AI integration using raw HTTP requests (no openai SDK required)."""
import json
import logging
import os
import requests
from datetime import datetime, timedelta
import time

# Serenity supply-chain chokepoint lens — PRIMARY decision framework
# (user directive 2026-06-08). Imported defensively so a missing skill never
# breaks stock analysis; if unavailable, the lens block is simply empty.
try:
    import serenity_lens
except Exception:  # pragma: no cover
    try:
        from . import serenity_lens
    except Exception:
        serenity_lens = None

logger = logging.getLogger(__name__)

# Model names are DB-configurable so you can switch without code changes:
#   sqlite> UPDATE settings SET value='qwen3.5:35b-a3b'
#           WHERE user_id=1 AND key='ollama_model';
# Defaults below are used only when the DB setting is absent.
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"  # was "deepseek-reasoner" (deprecated 2026-07-24)
DEFAULT_OLLAMA_MODEL   = "DRL70B:latest"     # DeepSeek-R1-Distill-Llama-70B Q4

# Backward-compatible module-level constants (legacy code paths still read these)
DEEPSEEK_MODEL = DEFAULT_DEEPSEEK_MODEL
OLLAMA_MODEL   = DEFAULT_OLLAMA_MODEL
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"


def _get_model_name(provider: str) -> str:
    """Resolve the active model name from DB settings, falling back to defaults.
    Avoids importing database at module load by lazy-importing here."""
    try:
        from database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            if provider == "deepseek_api":
                return get_setting(db, "deepseek_model", 1, DEFAULT_DEEPSEEK_MODEL)
            return get_setting(db, "ollama_model", 1, DEFAULT_OLLAMA_MODEL)
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[AI] model setting lookup failed, using default: {e}")
        return DEFAULT_DEEPSEEK_MODEL if provider == "deepseek_api" else DEFAULT_OLLAMA_MODEL


def _call_deepseek_api(api_key, messages, max_tokens=2000, temperature=0.1):
    """Make a raw HTTP call to DeepSeek Cloud API."""
    if not api_key:
        raise ValueError("Missing DeepSeek API Key")
    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }
    payload = {
        "model": _get_model_name("deepseek_api"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(DEEPSEEK_BASE_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _get_ollama_host() -> str:
    """Resolve Ollama host URL from DB setting (default: localhost:11434).
    Set to 'http://localhost:11435' to point at the user-level Ollama
    instance with newer models like qwen3.5:35b."""
    try:
        from database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            return get_setting(db, "ollama_host", 1, "http://localhost:11434").rstrip("/")
        finally:
            db.close()
    except Exception:
        return "http://localhost:11434"


def _get_lora_inference_url() -> str:
    """If non-empty, the LoRA-fine-tuned vLLM service is up — route through it.
    Set by the auto-deployment pipeline after a LoRA model passes validation."""
    try:
        from database import SessionLocal, get_setting
        db = SessionLocal()
        try:
            return get_setting(db, "lora_inference_url", 1, "").rstrip("/")
        finally:
            db.close()
    except Exception:
        return ""


def _call_lora_vllm(messages, temperature=0.1, base_url: str = ""):
    """OpenAI-compatible chat completion against the LoRA vLLM server."""
    url = f"{base_url}/chat/completions"
    payload = {
        "model": "alphatrader-lora",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2000,
    }
    resp = requests.post(url, json=payload, timeout=360)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _extract_signal_json(content: str) -> dict:
    """Robustly pull the final {signal,...} JSON object out of an LLM response.

    Handles four observed Qwen3.5/reasoning-model failure modes that the old
    greedy `re.search(r'(\\{.*\\})', ...)` regex choked on:
      1. Closed `<think>...</think>` blocks (older Qwen3 / DeepSeek-R1 style).
      2. Open-ended `Thinking Process:` / `## Reasoning` prefixes (Qwen3.5
         in open-thinking mode — NO <think> tags, just freeform reasoning).
      3. Truncated output where reasoning runs out of budget before the JSON
         section is reached (returns "no JSON found", not a crash).
      4. Multiple `{...}` blocks in the response (reasoning steps that
         contain JSON-ish fragments). Last balanced object wins — it's the
         model's final answer.

    Raises ValueError if no parseable JSON dict containing "signal" is found.
    """
    import re
    text = (content or "").strip()

    # 1. Strip closed <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # 2. Strip stray opener (truncated mid-think) — keep whatever follows it,
    #    or whatever preceded it if nothing follows
    if "<think>" in text:
        parts = text.split("<think>", 1)
        text = (parts[1] or parts[0]).strip()

    # 3. Detect reasoning-loop pathology: same line repeated 5+ times. Don't
    #    feed garbage downstream — fail fast so the caller logs a clean HOLD.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        from collections import Counter
        most_common, top_n = Counter(lines).most_common(1)[0]
        if top_n >= 5 and len(most_common) > 8:
            raise ValueError(
                f"Reasoning loop detected (line repeated {top_n}x): {most_common[:80]!r}"
            )

    # 4. Strip markdown code fence wrappers
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "").strip()

    if not text or "{" not in text:
        raise ValueError("No JSON found in response (likely truncated mid-reasoning)")

    # 5. Walk all balanced JSON objects; keep the last one that has a signal key.
    #    This survives "Thinking Process: ... { reasoning blob } ... final: { real answer }".
    decoder = json.JSONDecoder()
    candidates = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            try:
                obj, end = decoder.raw_decode(text, i)
                if isinstance(obj, dict):
                    candidates.append(obj)
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1

    # Prefer the last object containing a "signal" field; else the last dict.
    for obj in reversed(candidates):
        if "signal" in obj:
            return obj
    if candidates:
        return candidates[-1]

    raise ValueError("Could not extract valid JSON from response")


def _call_ollama(messages, temperature=0.1):
    """Make a raw HTTP call to the LLM backend.
    Routing priority:
      1. If lora_inference_url DB setting is non-empty → call LoRA vLLM
         (the fine-tuned, validated model from the MLOps pipeline)
      2. Otherwise → call regular Ollama at ollama_host
    On vLLM failure, automatically falls back to Ollama for resilience."""
    lora_url = _get_lora_inference_url()
    if lora_url:
        try:
            return _call_lora_vllm(messages, temperature, base_url=lora_url)
        except Exception as e:
            # vLLM failed — fall back to Ollama instead of crashing the trade
            import logging
            logging.getLogger(__name__).warning(f"[LoRA vLLM] failed, falling back to Ollama: {e}")

    url = f"{_get_ollama_host()}/api/chat"
    model_name = _get_model_name("ollama")

    # Reasoning models (Qwen3.5, DeepSeek-R1) emit a long internal "thinking"
    # trace before producing the final answer.  We don't want that for structured
    # JSON output: it wastes tokens, blows num_predict, and (when Qwen3 is in
    # "open-thinking" mode without <think> tags) pollutes the parser. Disable
    # thinking mode via Ollama's `think` flag — supported on Qwen3 family.
    is_reasoning = any(tag in model_name.lower() for tag in ("qwen3", "r1", "thinking"))
    num_predict = 6144 if is_reasoning else 2048   # bump fallback budget too

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        }
    }
    if is_reasoning:
        # Ollama >=0.4 honors this for hybrid models like Qwen3.5
        payload["think"] = False
    try:
        # Reasoning models are 3-5× slower than dense models — bump timeout
        timeout = 600 if is_reasoning else 360
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        msg = resp.json()["message"]
        content = (msg.get("content") or "").strip()
        # Reasoning-model fallback: if `content` is empty but `thinking` is
        # populated (generation truncated before the answer block), use the
        # thinking trace so downstream JSON parsers still get *something*.
        if not content and msg.get("thinking"):
            content = msg["thinking"]
        return content
    except requests.exceptions.ConnectionError:
        raise ConnectionError("Cannot connect to local Ollama. Is it running?")


def _score_news_freshness(news_items: list) -> list:
    """
    Score and sort news by freshness. Newer = higher priority.
    Returns list of (title, publisher, age_label, priority_tag) sorted by recency.
    """
    now = time.time()
    scored = []
    for n in (news_items or []):
        title = n.get("title", "")
        publisher = n.get("publisher", "")
        pub_time = n.get("published", 0) or n.get("providerPublishTime", 0)
        if not title:
            continue
        if pub_time > 0:
            hours_ago = (now - pub_time) / 3600
        else:
            hours_ago = 24  # unknown age → treat as old
        if hours_ago < 2:
            tag = "BREAKING"
            age = f"{hours_ago * 60:.0f}min ago"
        elif hours_ago < 6:
            tag = "RECENT"
            age = f"{hours_ago:.0f}h ago"
        elif hours_ago < 24:
            tag = "TODAY"
            age = f"{hours_ago:.0f}h ago"
        else:
            tag = "OLD"
            age = f"{hours_ago / 24:.0f}d ago"
        scored.append((hours_ago, title, publisher, age, tag))

    scored.sort(key=lambda x: x[0])  # newest first
    return scored


def analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context="", upcoming_events="", rl_lessons="", sector="Other", global_context=None, catalysts=None):
    """Use DeepSeek-R1 (Local or API) to analyze a stock and generate trading signal."""
    if ai_provider == "deepseek_api" and not api_key:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "target_price": None,
            "stop_loss": None,
            "reasoning": "未配置 DeepSeek API Key。请在设置页面中添加 API Key，或切换为本地大模型 (Ollama)。",
            "model": _get_model_name("deepseek_api"),
            "timestamp": datetime.utcnow().isoformat()
        }

    try:
        recent_prices = []
        if history:
            for bar in history[-10:]:
                recent_prices.append(
                    "  {}: O={} H={} L={} C={} V={:,}".format(
                        datetime.fromtimestamp(bar["time"]).strftime("%Y-%m-%d"),
                        bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]
                    )
                )
        price_summary = "\n".join(recent_prices) if recent_prices else "No historical data"

        scored_news = _score_news_freshness(news)
        news_lines = []
        for _, title, pub, age, tag in scored_news[:8]:
            prefix = f"**[{tag}]**" if tag in ("BREAKING", "RECENT") else f"[{tag}]"
            news_lines.append(f"  - {prefix} {title} ({pub}, {age})")
        news_summary = "\n".join(news_lines) if news_lines else "No recent news"

        # Catalysts block — news_intelligence.detect_catalysts_for_symbol() output.
        # CRITICAL: this was previously dropped silently before being passed to AI,
        # causing earnings beats and other major events to be invisible to the model.
        cat_lines = []
        for c in (catalysts or []):
            level = c.get("catalyst_level", "MILD")
            title = c.get("news_title", "")
            kws = ", ".join(c.get("matched_keywords", []) or [])
            thesis = c.get("upside_thesis", "")
            cat_lines.append(f"  - **[{level}]** {title}")
            if kws:
                cat_lines.append(f"      keywords: {kws}")
            if thesis:
                cat_lines.append(f"      thesis: {thesis}")
        catalysts_block = "\n".join(cat_lines) if cat_lines else "  (no active catalysts detected)"

        # Valuation block — DCF/DDM data are KNOWN to be unreliable for many tickers
        # (random low/high outliers from yfinance scraping). If the gap is more
        # extreme than ±30%, hide the numbers so AI doesn't anchor on garbage.
        cp_val = quote.get("current") or 0
        iv_val = quote.get("intrinsic_value")
        dcf_val = quote.get("dcf_value")
        ddm_val = quote.get("ddm_value")
        vgap_raw = quote.get("valuation_gap_pct") or 0
        vgap_pct = vgap_raw * 100
        # Tighten the sanity band for non-US tickers (HK/CN/JP/UK/etc.) — yfinance's
        # quoteSummary fundamentals are notoriously incomplete/wrong-currency for
        # non-US listings, so DCF on a HK name is much more likely to be garbage.
        # 2026-05-28: 舜宇 2382.HK passed the 0.3x-3x gate at iv=$24.93 / px=$75.15
        # (ratio 0.332) and the resulting "+67% overvalued" DCF anchor pushed Gemma4
        # to HOLD a PE-16 leader. Bump non-US lower bound to 0.5x → hides poison
        # DCF and tells the AI to rely on PE / technicals instead.
        _is_non_us = isinstance(symbol, str) and "." in symbol and len(symbol.rsplit(".", 1)[-1]) >= 2
        _lo, _hi = (0.5, 2.0) if _is_non_us else (0.3, 3.0)
        is_sane = (
            iv_val is not None and iv_val > 0
            and cp_val > 0
            and _lo * cp_val <= iv_val <= _hi * cp_val
        )
        if is_sane:
            valuation_block = (
                f"- DCF Intrinsic Value: ${dcf_val}\n"
                f"- DDM Intrinsic Value: ${ddm_val}\n"
                f"- Final Blended Intrinsic Value: ${iv_val}\n"
                f"- Valuation Gap: {vgap_pct:+.2f}% (Negative = Undervalued = BUY opportunity)\n"
                f"*Decision Rule: If Valuation Gap is significantly negative (<-10%) AND technicals are bullish, favor BUY.*\n"
                f"*If overvalued but we don't hold it → HOLD (we cannot short).*"
            )
        else:
            valuation_block = (
                "- DCF / DDM data UNRELIABLE for this ticker (data-source outlier). "
                "Do NOT use absolute valuation; rely on P/E, 52w range, technicals, "
                "and catalysts instead. Default-prior is NEUTRAL on fundamentals."
            )
        
        # Enhanced Indicators with Over-extension logic
        ind_data = indicators.copy() if indicators else {}
        rsi_state = ind_data.get("rsi_state", "NEUTRAL")
        dist_ma200 = ind_data.get("dist_from_ma200_pct", 0)
        
        overextension_msg = ""
        if rsi_state == "OVERBOUGHT" or dist_ma200 > 15:
            overextension_msg = (
                "\n⚠️ WARNING: This stock is technically OVEREXTENDED (RSI: {}, MA200 Dist: {:.1f}%). "
                "The 'High Point' risk is elevated. Favor mean-reversion caution over momentum chasing."
            ).format(ind_data.get("rsi"), dist_ma200)

        ind_summary = json.dumps(ind_data, indent=2) if ind_data else "Not available"

        # Build global market context section
        global_ctx_section = ""
        if global_context and isinstance(global_context, dict):
            narrative = global_context.get("ai_narrative", "")
            if narrative:
                global_ctx_section = "\n" + narrative

        # Serenity supply-chain chokepoint lens — the PRIMARY framework that leads
        # the decision. Built per-symbol so it carries his actual stance + dated
        # calls on this ticker, or hands the model his checklist for fresh names.
        serenity_block = ""
        if serenity_lens is not None:
            try:
                serenity_block = serenity_lens.build_serenity_lens_block(symbol, sector)
            except Exception as e:
                logger.warning("serenity_lens block failed for %s: %s", symbol, e)

        prompt = """You are an expert quantitative stock analyst advising a LONG-ONLY small-account trader.
CRITICAL CONSTRAINT: This account does NOT support short selling. Never output SHORT or COVER.

{serenity_lens}
---
*The sections below (news, strategy rules, technicals, valuation, RL feedback) are SUPPORTING evidence. Use them to pressure-test the Serenity chokepoint thesis above — not to override it.*

## NEWS PRIORITY RULES (MOST IMPORTANT)
- **[BREAKING] and [RECENT] news ALWAYS override ongoing geopolitical narratives.**
- A major layoff, new AI model release, earnings surprise, or M&A announcement happening NOW is worth 10x more than a weeks-old geopolitical scenario.
- Old ongoing events (wars, tariffs, sanctions) should be treated as BACKGROUND CONTEXT only — they lower confidence slightly but should NOT dominate your decision.
- If breaking news directly affects this stock, act on it aggressively (higher confidence).
- If no breaking news affects this stock, evaluate purely on fundamentals + technicals.

Strategy Rules:
1. Find the best BUY opportunities with a strong emphasis on "Buy Low, Sell High" (mean-reversion combined with trend).
2. DO NOT CHASE: Avoid buying stocks that are heavily overextended or have already experienced massive near-term rallies. Look for healthy pullbacks to support levels or moving averages within an uptrend.
3. Only output SELL if we currently hold this stock and should take profits or cut losses.
4. If a stock looks overvalued or overextended but we don't hold it, output HOLD — never SHORT.
5. **LET WINNERS RUN** (user directive 2026-05-24): If we already own this stock AND it is trending up
   (price > MA50 AND RSI between 50-75 AND no breaking bearish catalyst), output HOLD even if extended.
   DO NOT SELL on minor pullbacks (-3% intraday is normal in an uptrend). Sell triggers should ONLY
   be: (a) stop_loss hit (price < entry × 0.95), (b) breakdown below MA50, (c) STRONG BEARISH catalyst,
   (d) RSI > 80 + bearish divergence. Multi-bagger stocks (SNDK-class, +500%+ run) need conviction to hold —
   selling on every wiggle = leaving 10x gains on the table.
6. **DISCOVERY BIAS**: This stock made it into the watchlist via dynamic momentum / news / sentiment
   discovery — not because we hand-picked it. Treat that as a positive prior: market thinks something
   is happening. Demand harder evidence to issue SELL/HOLD than to issue BUY on a trending name.

## ⏳ LONG-TERM HORIZON MANDATE (CRITICAL — User directive 2026-05-26)
- **THINK LIKE A LONG-TERM INVESTOR** (Buffett/Munger/Lynch), NOT a day-trader.
  Evaluate businesses on a 6-24 month horizon. Quality compounders are meant to
  be HELD FOR YEARS, not flipped weekly.
- **DO NOT CHURN QUALITY NAMES**: NVDA/AAPL/MSFT/GOOGL/TSLA/AMZN-class businesses
  should almost always be HOLD once owned. The user explicitly complained about
  these being bought-and-sold within a week. That destroys returns via spread +
  slippage + missed compounding.
- **SELL bar is VERY HIGH** — only output SELL on a held position if ONE of:
    (a) the fundamental investment thesis is broken (not just price moved),
    (b) a hard stop-loss is genuinely breached (real loss > stop %, not daily noise),
    (c) there is a dramatically better opportunity AND we have no cash.
  A stock being "up a lot" or "slightly pulled back" or "looks extended" is NOT
  a sell reason. When in doubt on a quality holding → HOLD.
- **Daily/weekly under-performance vs S&P is NOISE** — ignore it. Do not trade
  to chase index tracking. Compounding happens over years.

## 🎯 FOCUS SECTORS (CRITICAL — User directive 2026-05-27)
- The user's CURRENT conviction themes are: **semiconductors / chips, GPU
  downstream supply chain (HBM memory, optical, power, equipment, packaging),
  physical AI / robotics**. These sectors are the TOP PRIORITY for new capital.
- When analyzing a stock IN these sectors: apply your full financial analysis
  (valuation, growth, technicals, catalysts) and BUY the best ones on healthy
  setups. These are where the portfolio should be concentrated.
- When analyzing a stock OUTSIDE these sectors (and not a core index ETF):
  default to HOLD unless it is an exceptional, high-conviction opportunity.
  Do NOT recommend buying generic off-theme large-caps just to "do something" —
  the user explicitly does not want capital diluted into off-theme names.

## PORTFOLIO STYLE NOTES (SUPPORTING — subordinate to the Serenity lens above)
- **SUPERSEDED (2026-06-08): the old "large-cap first / avoid small-cap" rule is DEMOTED.** The Serenity chokepoint lens leads now, and it explicitly PREFERS overlooked upstream small/mid-cap bottlenecks within the focus themes. Do NOT reject a name merely for being small or obscure — vet it against Serenity's checklist instead.
- **Liquidity still matters**: for genuinely thin micro-caps, require a real chokepoint thesis + a dated catalyst, and size to research depth (small starter positions), because exit liquidity is a real risk Serenity himself flags.
- **IGNORE WAR SCENARIOS**: Geopolitical events (Iran-US war, sanctions, etc.) should NOT drive BUY decisions. Focus on supply-chain bottlenecks, tech fundamentals, earnings, and sector momentum.
- **Mega-cap "shovel sellers" (NVDA-class)**: fine to HOLD if already owned (long-term mandate), but per the Serenity lens they are usually NOT where new capital finds the most mispricing — favor the upstream chokepoint instead.

## Stock: {symbol}

### Current Quote
- Price: ${current} | Change: {change:+.2f} ({change_pct:+.2f}%)
- High: ${high} | Low: ${low} | Volume: {volume:,}
- Market Cap: {mktcap} | P/E: {pe} | Sector: {sector}
- 52W: ${wklow} - ${wkhigh}

### Quantitative & Fundamental Valuation
{valuation_block}

### Market Microstructure & Flow (Smart Money Proxy)
- Volume Price Analysis (VPA): {vpa_signal}
- Volume Ratio (vs 20d avg): {vpa_vol_ratio}x
- Market Liquidity: {liquidity}
- Trade Crowding Risk: {crowding} (0.0=Low, 1.0=Extreme)

### Technical Indicators & Mean Reversion Risk
{indicators}
{overextend}

### Recent Price Action (Last 10 Sessions)
{prices}

### Lessons from Past Performance (RL Ground Truth)
{rl_feedback}

### Active Catalysts (from news_intelligence engine — already filtered for relevance)
{catalysts_block}

### Recent News (raw headlines)
{news}

{events}

{ctx}
{global_ctx}
Respond ONLY with valid JSON (no markdown):
{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": <float 0.5 to 1.0>,
  "target_price": <float or null>,
  "stop_loss": <float or null>,
  "recommended_weight_pct": <float 0.0 to 0.3>,
  "time_horizon": "short-term" | "medium-term" | "long-term",
  "key_factors": ["factor1", "factor2", "factor3"],
  "risks": ["risk1", "risk2"],
  "reasoning": "<detailed analysis 2-3 paragraphs focusing on long-only BUY opportunity or why to HOLD/SELL existing position>"
}}""".format(
            serenity_lens=serenity_block,
            symbol=symbol,
            current=quote.get("current", "N/A"),
            change=quote.get("change", 0),
            change_pct=quote.get("change_pct", 0),
            high=quote.get("high", "N/A"),
            low=quote.get("low", "N/A"),
            volume=quote.get("volume", 0),
            mktcap=quote.get("market_cap", "N/A"),
            pe=quote.get("pe_ratio", "N/A"),
            sector=sector,
            wklow=quote.get("fifty_two_week_low", "N/A"),
            wkhigh=quote.get("fifty_two_week_high", "N/A"),
            valuation_block=valuation_block,
            catalysts_block=catalysts_block,
            vpa_signal=quote.get("vpa_signal", "N/A"),
            vpa_vol_ratio=quote.get("vpa_volume_ratio", "N/A"),
            liquidity=quote.get("liquidity", "N/A"),
            crowding=quote.get("crowding", "N/A"),
            indicators=ind_summary,
            overextend=overextension_msg,
            prices=price_summary,
            rl_feedback=rl_lessons if rl_lessons else "No prior reinforcement learning feedback available yet.",
            news=news_summary,
            events=upcoming_events if upcoming_events else "",
            ctx="### Portfolio Context\n{}".format(portfolio_context) if portfolio_context else "",
            global_ctx=global_ctx_section
        )

        # /no_think is a Qwen3.x directive — harmless on other models, decisive
        # for Qwen3.5 where it suppresses the reasoning trace at decode time.
        messages = [
            {"role": "system", "content": (
                "You are a world-class quantitative analyst. "
                "Respond with valid JSON only, no markdown code blocks, no commentary. "
                "/no_think"
            )},
            {"role": "user", "content": prompt}
        ]

        if ai_provider == "ollama":
            content = _call_ollama(messages, temperature=0.1)
            used_model = _get_model_name("ollama")
        else:
            # Reasoning models on the cloud side (DeepSeek-V4-pro etc.) also need
            # a bigger budget so the final JSON isn't cut off after reasoning.
            content = _call_deepseek_api(api_key, messages, max_tokens=4000, temperature=0.1)
            used_model = _get_model_name("deepseek_api")

        result = _extract_signal_json(content)

        result["model"] = used_model
        result["timestamp"] = datetime.utcnow().isoformat()
        result["symbol"] = symbol
        return result

    except json.JSONDecodeError as e:
        logger.error("JSON parse error: %s\nContent: %s", e, content)
        return {
            "signal": "HOLD", "confidence": 0.0, "target_price": None, "stop_loss": None,
            "reasoning": "AI 响应解析错误，未返回有效JSON。请重试。",
            "model": used_model if 'used_model' in locals() else "unknown", "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error("AI error: %s", e)
        return {
            "signal": "HOLD", "confidence": 0.0, "target_price": None, "stop_loss": None,
            "reasoning": "分析出错: {}".format(str(e)),
            "model": used_model if 'used_model' in locals() else "unknown", "timestamp": datetime.utcnow().isoformat()
        }


def analyze_portfolio(ai_provider, api_key, positions, market_data):
    """Portfolio-level analysis using DeepSeek-R1."""
    if not positions:
        return {"suggestions": [], "overall_assessment": "暂无持仓可供分析。"}
    if ai_provider == "deepseek_api" and not api_key:
        return {"suggestions": [], "overall_assessment": "请配置 API Key 或切换至本地 Ollama 大模型后再分析。"}
    try:
        prompt = """As a portfolio manager, analyze this portfolio and provide rebalancing suggestions.

## Current Portfolio
{}

## Market Conditions
{}

Respond ONLY with valid JSON:
{{
  "overall_assessment": "<2-3 sentence summary>",
  "portfolio_score": <int 1-10>,
  "diversification_rating": "Poor|Fair|Good|Excellent",
  "risk_level": "Low|Medium|High|Very High",
  "suggestions": [{{"action": "BUY|SELL|REBALANCE", "symbol": "...", "reason": "...", "urgency": "Low|Medium|High"}}],
  "sector_analysis": "<sector analysis>"
}}""".format(json.dumps(positions, indent=2), json.dumps(market_data, indent=2))

        # Same /no_think directive as analyze_stock — suppress Qwen3 reasoning
        # trace so the response is just the JSON we want.
        messages = [{"role": "system", "content": (
                        "You are a portfolio manager. Respond with valid JSON only. /no_think"
                    )},
                    {"role": "user", "content": prompt}]

        if ai_provider == "ollama":
            content = _call_ollama(messages, temperature=0.1)
        else:
            # Bumped 1500 → 3000 to give reasoning models room for thoughts+JSON
            content = _call_deepseek_api(api_key, messages, max_tokens=3000, temperature=0.1)

        # Reuse the same robust extractor analyze_stock uses — handles closed
        # <think> tags, open "Thinking Process:" prefixes, multi-JSON outputs,
        # reasoning loops, and markdown fences uniformly.
        return _extract_signal_json(content)
    except Exception as e:
        logger.error("Portfolio analysis error: %s", e)
        return {"suggestions": [], "overall_assessment": "分析出错: {}".format(str(e))}


def chat_with_ai(ai_provider, api_key, messages, context=""):
    """General market chat with DeepSeek-R1."""
    if ai_provider == "deepseek_api" and not api_key:
        return "请在设置页面中配置 DeepSeek API Key，或切换为本地大模型（Ollama）后使用 AI 助手功能。"
    try:
        system_msg = (
            "You are an expert financial analyst and trading advisor with deep knowledge of global markets. "
            "Provide clear, actionable insights in Chinese. Always include appropriate risk disclaimers."
        )
        if context:
            system_msg += "\n\nCurrent portfolio context:\n{}".format(context)
            
        msgs = [{"role": "system", "content": system_msg}] + messages
        if ai_provider == "ollama":
            content = _call_ollama(msgs, temperature=0.3)
            # format reasoning tags if present
            content = content.replace("<think>", "🤔 **思考过程:**\n> _").replace("</think>", "_\n\n---\n")
            return content
        else:
            return _call_deepseek_api(api_key, msgs, max_tokens=1000)
    except Exception as e:
        logger.error("Chat error: %s", e)
        return "与 AI 引擎通信出错: {}".format(str(e))
