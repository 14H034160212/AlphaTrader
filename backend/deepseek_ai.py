"""DeepSeek-R1 AI integration using raw HTTP requests (no openai SDK required)."""
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-reasoner"  # DeepSeek API Model
OLLAMA_MODEL = "deepseek-r1:14b"      # Local Ollama Model
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"


def _call_deepseek_api(api_key, messages, max_tokens=2000, temperature=0.1):
    """Make a raw HTTP call to DeepSeek Cloud API."""
    if not api_key:
        raise ValueError("Missing DeepSeek API Key")
    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(DEEPSEEK_BASE_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_ollama(messages, temperature=0.1):
    """Make a raw HTTP call to local Ollama API."""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature
        }
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]
    except requests.exceptions.ConnectionError:
        raise ConnectionError("Cannot connect to local Ollama. Is it running?")


def analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context=""):
    """Use DeepSeek-R1 (Local or API) to analyze a stock and generate trading signal."""
    if ai_provider == "deepseek_api" and not api_key:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "target_price": None,
            "stop_loss": None,
            "reasoning": "未配置 DeepSeek API Key。请在设置页面中添加 API Key，或切换为本地大模型 (Ollama)。",
            "model": DEEPSEEK_MODEL,
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

        news_items = ["  - {} ({})".format(n.get("title",""), n.get("publisher","")) for n in news[:5]]
        news_summary = "\n".join(news_items) if news_items else "No recent news"
        ind_summary = json.dumps(indicators, indent=2) if indicators else "Not available"

        prompt = """You are an expert quantitative stock analyst. Analyze the following stock data and provide a trading recommendation.

## Stock: {symbol}

### Current Quote
- Price: ${current} | Change: {change:+.2f} ({change_pct:+.2f}%)
- High: ${high} | Low: ${low} | Volume: {volume:,}
- Market Cap: {mktcap} | P/E: {pe} | Sector: {sector}
- 52W: ${wklow} - ${wkhigh}

### Technical Indicators
{indicators}

### Recent Price Action (Last 10 Sessions)
{prices}

### Recent News
{news}

{ctx}

Respond ONLY with valid JSON (no markdown):
{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": <float 0.0-1.0>,
  "target_price": <float or null>,
  "stop_loss": <float or null>,
  "time_horizon": "short-term" | "medium-term" | "long-term",
  "key_factors": ["factor1", "factor2", "factor3"],
  "risks": ["risk1", "risk2"],
  "reasoning": "<detailed analysis 2-3 paragraphs>"
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
            sector=quote.get("sector", "N/A"),
            wklow=quote.get("fifty_two_week_low", "N/A"),
            wkhigh=quote.get("fifty_two_week_high", "N/A"),
            indicators=ind_summary,
            prices=price_summary,
            news=news_summary,
            ctx="### Portfolio Context\n{}".format(portfolio_context) if portfolio_context else ""
        )

        messages = [
            {"role": "system", "content": "You are a world-class quantitative analyst. Respond with valid JSON only, no markdown code blocks."},
            {"role": "user", "content": prompt}
        ]

        if ai_provider == "ollama":
            content = _call_ollama(messages, temperature=0.1)
            used_model = OLLAMA_MODEL
        else:
            content = _call_deepseek_api(api_key, messages, max_tokens=2000, temperature=0.1)
            used_model = DEEPSEEK_MODEL

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
