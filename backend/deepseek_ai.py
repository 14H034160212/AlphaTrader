"""DeepSeek-R1 AI integration using raw HTTP requests (no openai SDK required)."""
import json
import logging
import os
import requests
from datetime import datetime, timedelta
import time

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
    # trace before producing the final answer.  Without a big num_predict
    # budget they get cut off mid-think and `content` comes back empty.
    is_reasoning = any(tag in model_name.lower() for tag in ("qwen3", "r1", "thinking"))
    num_predict = 4096 if is_reasoning else 2048

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        }
    }
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


def analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context="", upcoming_events="", rl_lessons="", sector="Other", global_context=None):
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

        prompt = """You are an expert quantitative stock analyst advising a LONG-ONLY small-account trader.
CRITICAL CONSTRAINT: This account does NOT support short selling. Never output SHORT or COVER.

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

## PORTFOLIO STYLE MANDATE (CRITICAL — User explicit directive)
- **LARGE-CAP FIRST**: Strongly prefer well-known, large-cap tech companies: NVDA, AAPL, MSFT, AMZN, TSLA, GOOGL, META, AMD, BABA, JD, NTES, QQQ, SPY, etc.
- **AVOID SMALL/UNKNOWN**: Small or obscure companies consistently underperform in this portfolio. Only recommend them if confidence ≥ 0.85 AND fundamentals are exceptional.
- **IGNORE WAR SCENARIOS**: Geopolitical events (Iran-US war, sanctions, etc.) should NOT drive BUY decisions. Focus on tech fundamentals, earnings, and sector momentum.
- **TECH SECTOR BIAS**: The portfolio target is 45% satellite in high-quality tech. When in doubt, favor blue-chip tech names over anything else.

## Stock: {symbol}

### Current Quote
- Price: ${current} | Change: {change:+.2f} ({change_pct:+.2f}%)
- High: ${high} | Low: ${low} | Volume: {volume:,}
- Market Cap: {mktcap} | P/E: {pe} | Sector: {sector}
- 52W: ${wklow} - ${wkhigh}

### Quantitative & Fundamental Valuation
- DCF Intrinsic Value: ${dcf}
- DDM Intrinsic Value: ${ddm}
- Final Blended Intrinsic Value: ${intrinsic}
- Valuation Gap: {val_gap_pct:.2f}% (Negative = Undervalued = BUY opportunity)
*Decision Rule: If Valuation Gap is significantly negative (<-10%) AND technicals are bullish, favor BUY.*
*If overvalued but we don't hold it → HOLD (we cannot short).*

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

### Recent News
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
            dcf=quote.get("dcf_value", "N/A"),
            ddm=quote.get("ddm_value", "N/A"),
            intrinsic=quote.get("intrinsic_value", "N/A"),
            val_gap_pct=quote.get("valuation_gap_pct", 0) * 100 if quote.get("valuation_gap_pct") else 0,
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

        messages = [
            {"role": "system", "content": "You are a world-class quantitative analyst. Respond with valid JSON only, no markdown code blocks."},
            {"role": "user", "content": prompt}
        ]

        if ai_provider == "ollama":
            content = _call_ollama(messages, temperature=0.1)
            used_model = _get_model_name("ollama")
        else:
            content = _call_deepseek_api(api_key, messages, max_tokens=2000, temperature=0.1)
            used_model = _get_model_name("deepseek_api")

        # Strip markdown if present
        content = content.strip()
        # Clean reasoning tags '<think>...</think>' generated by local models
        if "<think>" in content and "</think>" in content:
            content = content.split("</think>")[-1].strip()
            
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON if there's text around it
            import re
            json_match = re.search(r'(\{.*\})', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(1))
            else:
                raise ValueError("Could not extract valid JSON from response")

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

        messages = [{"role": "system", "content": "You are a portfolio manager. Respond with valid JSON only."},
                    {"role": "user", "content": prompt}]

        if ai_provider == "ollama":
            content = _call_ollama(messages, temperature=0.1)
        else:
            content = _call_deepseek_api(api_key, messages, max_tokens=1500, temperature=0.1)
            
        content = content.strip()
        if "<think>" in content and "</think>" in content:
            content = content.split("</think>")[-1].strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
        return json.loads(content)
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
