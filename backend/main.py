"""
FastAPI main application - REST API + WebSocket server for global stp.
"""
from __future__ import annotations
from typing import List, Optional, Dict
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import market_data as md
import deepseek_ai as ai
import event_monitor as em
import news_intelligence as ni
import rl_data_collector as rl
import social_sentiment as ss
import blog_monitor as bm
import kronos_analysis as ka
import notifier
from trading_engine import TradingEngine
from database import create_tables, get_db, get_setting, set_setting, Trade, AISignal, WatchedStock, Settings, User
from auth import get_current_user, create_access_token, get_password_hash, verify_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Active WebSocket connections
active_connections: List[WebSocket] = []
# Cache for latest prices (symbol -> price)
price_cache: Dict = {}
# Cache for market indices
market_cache: Dict = {}
last_market_fetch = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    create_tables()
    # Pre-load Kronos model onto A100 GPU at startup (avoid cold-start delay in trade loop)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ka.preload_model)
    task1 = asyncio.create_task(background_price_refresh())
    task2 = asyncio.create_task(background_auto_trade_loop())
    task3 = asyncio.create_task(background_event_scan())
    task4 = asyncio.create_task(background_news_scan())
    task5 = asyncio.create_task(background_social_sentiment_scan())
    task6 = asyncio.create_task(background_blog_scan())
    task7 = asyncio.create_task(background_daily_summary())
    logger.info("Background tasks started: price_refresh + auto_trade_loop + event_scan + news_scan + social_sentiment + blog_monitor + kronos_gpu + daily_digest")
    yield
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()
    task5.cancel()
    task6.cancel()
    task7.cancel()
    logger.info("Shutting down trading platform")


app = FastAPI(
    title="Global stp",
    description="AI-powered stock market tracker and automated trading platform using DeepSeek-R1",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


# ─────────────────────────────────────────────
# Pydantic request models
# ─────────────────────────────────────────────

class TradeRequest(BaseModel):
    symbol: str
    side: str  # BUY or SELL
    quantity: float
    price: Optional[float] = None  # If None, use live price

class AnalyzeRequest(BaseModel):
    symbol: str

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]

class SettingsUpdate(BaseModel):
    key: str
    value: str

class WatchlistUpdate(BaseModel):
    symbol: str
    action: str  # "add" or "remove"

class OpenClawWebhook(BaseModel):
    command: str
    symbol: Optional[str] = None
    group_id: Optional[str] = None
    sender: Optional[str] = None

class UserRegister(BaseModel):
    username: str
    password: str
    email: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class TransferRequest(BaseModel):
    amount: float
    type: str # DEPOSIT or WITHDRAW
    
def build_rich_portfolio_context(db, user_id: int, engine) -> str:
    """
    Build a comprehensive portfolio context string for the AI, including:
    - Current positions with cost basis, P&L, and % change since entry
    - Recent trade history (last 10 trades)
    - Overall portfolio performance summary
    This helps the AI make informed decisions based on what has already been bought/sold.
    """
    from datetime import datetime, timedelta
    lines = []

    # ── Portfolio Summary ─────────────────────────────────────────────────────
    summary = engine.get_portfolio_summary()
    equity = summary.get("total_equity", 0)
    cash = summary.get("cash", 0)
    invested = equity - cash
    ret = summary.get("total_return", 0) or 0
    ret_pct = summary.get("total_return_pct", 0) or 0
    lines.append("### Portfolio State")
    lines.append(f"- Total Equity: ${equity:,.2f}")
    lines.append(f"- Cash Available: ${cash:,.2f}  ({100*cash/equity:.0f}% of portfolio)" if equity else f"- Cash: ${cash:,.2f}")
    lines.append(f"- Invested: ${invested:,.2f}")
    lines.append(f"- Total P&L: ${ret:+,.2f} ({ret_pct:+.2f}%)")

    # ── Current Positions ─────────────────────────────────────────────────────
    positions = summary.get("positions", [])
    if positions:
        lines.append("\n### Current Holdings")
        for p in sorted(positions, key=lambda x: abs(x.get("market_value", 0)), reverse=True):
            sym = p.get("symbol", "?")
            qty = p.get("quantity", 0)
            entry = p.get("avg_cost", p.get("current_price", 0))
            cur = p.get("current_price", 0)
            pnl = p.get("unrealized_pnl", 0) or 0
            pnl_pct = ((cur - entry) / entry * 100) if entry else 0
            mv = p.get("market_value", qty * cur)
            lines.append(
                f"- {sym}: {qty:.4f} shares | Entry ${entry:.2f} → Now ${cur:.2f} "
                f"({pnl_pct:+.1f}%) | P&L ${pnl:+.2f} | Value ${mv:.2f}"
            )
    else:
        lines.append("\n### Current Holdings: None (100% cash)")

    # ── Recent Trade History ──────────────────────────────────────────────────
    recent_trades = db.query(Trade).filter(
        Trade.user_id == user_id
    ).order_by(Trade.timestamp.desc()).limit(15).all()

    if recent_trades:
        lines.append("\n### Recent Trade History (last 15 trades)")
        for t in recent_trades:
            age_hours = (datetime.utcnow() - t.timestamp).total_seconds() / 3600 if t.timestamp else 0
            age_str = f"{age_hours:.0f}h ago" if age_hours < 48 else f"{age_hours/24:.0f}d ago"
            lines.append(
                f"- [{age_str}] {t.side} {t.symbol} × {t.quantity:.4f} @ ${t.price:.2f}"
                f" = ${t.total_value:.2f}"
            )
            if t.reasoning:
                lines.append(f"  Reason: {t.reasoning[:100]}")
    else:
        lines.append("\n### Recent Trade History: No trades yet")

    # ── Performance Note ──────────────────────────────────────────────────────
    today_trades = [t for t in recent_trades if t.timestamp and
                    (datetime.utcnow() - t.timestamp).total_seconds() < 86400]
    lines.append(f"\n### Session Stats")
    lines.append(f"- Trades today: {len(today_trades)}")
    lines.append(f"- Total trades on record: {len(recent_trades)}")

    lines.append(
        "\nINSTRUCTION: Use this history to avoid re-buying a stock just sold at a loss, "
        "avoid over-concentrating in one sector, and factor in existing P&L when sizing positions."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Background price refresh
# ─────────────────────────────────────────────

async def background_price_refresh():
    """Continuously refresh prices and broadcast to WebSocket clients."""
    global price_cache, market_cache, last_market_fetch
    while True:
        try:
            db = next(get_db())
            # Get all unique symbols from all users' watchlists and positions
            symbols_to_track = set(md.DEFAULT_WATCHLIST)
            users = db.query(User).all()
            for user in users:
                watchlist_json = get_setting(db, "watchlist", user.id, "[]")
                try:
                    watchlist = json.loads(watchlist_json)
                    symbols_to_track.update(watchlist)
                except: pass

                engine = TradingEngine(db, user.id)
                positions = engine.get_all_positions()
                symbols_to_track.update([p.symbol for p in positions])

            all_symbols = list(symbols_to_track)

            # Fetch prices in executor (non-blocking yfinance calls)
            loop = asyncio.get_event_loop()
            new_prices = {}
            async def _fetch_quote(sym):
                try:
                    q = await loop.run_in_executor(None, md.get_stock_quote, sym)
                    if q:
                        new_prices[sym] = q["current"]
                        price_cache[sym] = q
                except Exception as e:
                    logger.error(f"Error fetching {sym}: {e}")
            await asyncio.gather(*[_fetch_quote(s) for s in all_symbols[:20]])

            # Update position prices for each user
            if new_prices:
                for user in users:
                    engine = TradingEngine(db, user.id)
                    engine.update_position_prices(new_prices)

            # Refresh market indices every 5 minutes
            now = datetime.utcnow()
            if last_market_fetch is None or (now - last_market_fetch).seconds > 300:
                try:
                    market_cache = md.get_all_indices()
                    last_market_fetch = now
                except Exception as e:
                    logger.error(f"Market fetch error: {e}")

            # Broadcast to all WebSocket clients
            await broadcast({
                "type": "price_update",
                "prices": new_prices,
                "timestamp": datetime.utcnow().isoformat()
            })

        except Exception as e:
            logger.error(f"Background refresh error: {e}")

        await asyncio.sleep(30)


async def background_auto_trade_loop():
    """Continuously analyze watchlist and trigger auto-trades for all users."""
    await asyncio.sleep(5)  # Let server fully start before first heavy cycle
    while True:
        try:
            loop = asyncio.get_event_loop()
            db = next(get_db())
            users = db.query(User).all()

            for user in users:
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                logger.info(f"Starting auto-trade cycle for user: {user.username}")
                api_key = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "deepseek_api")
                watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                watchlist = json.loads(watchlist_json)

                engine = TradingEngine(db, user.id)
                portfolio_context = await loop.run_in_executor(
                    None, build_rich_portfolio_context, db, user.id, engine
                )

                # Run all slow blocking I/O in executor so event loop stays free for HTTP requests
                event_context = await loop.run_in_executor(None, lambda: em.build_event_context(watchlist, days_ahead=7))
                threat_map = await loop.run_in_executor(None, lambda: ni.scan_all_threats(watchlist, hours_back=24))
                active_macros = await loop.run_in_executor(None, lambda: ni.detect_active_macro_scenarios(hours_back=6))
                macro_context = ni.build_macro_scenario_context(active_macros)
                blog_alerts = await loop.run_in_executor(None, lambda: bm.scan_all_blogs(hours_back=12))

                for symbol in watchlist:
                    try:
                        await asyncio.sleep(1)  # yield to event loop between symbols
                        quote = price_cache.get(symbol) or await loop.run_in_executor(None, md.get_stock_quote, symbol)
                        if not quote: continue

                        history, indicators, news = await asyncio.gather(
                            loop.run_in_executor(None, md.get_stock_history, symbol, "6mo"),
                            loop.run_in_executor(None, md.get_technical_indicators, symbol),
                            loop.run_in_executor(None, md.get_stock_news, symbol),
                        )

                        # Kronos K-line forecast (A100 GPU) — already in executor
                        kronos_pred = await loop.run_in_executor(None, ka.predict_next_candles, symbol, history)
                        kronos_context = ka.build_kronos_context(kronos_pred)

                        # Merge all intelligence layers
                        threats = threat_map.get(symbol, [])
                        threat_context = ni.build_threat_context(symbol, threats)
                        sentiment_context = ss.build_sentiment_context(symbol)
                        blog_context = bm.build_blog_alert_context(blog_alerts, target_symbol=symbol)
                        full_context = "\n\n".join(filter(None, [event_context, threat_context, macro_context, sentiment_context, blog_context, kronos_context]))

                        # AI analysis in executor (Ollama HTTP call — can take 30-60s)
                        signal = await loop.run_in_executor(
                            None, ai.analyze_stock,
                            ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context, full_context
                        )

                        # Record to RL training dataset
                        rl.record_signal_state(signal, quote, indicators or {}, full_context, portfolio_context)

                        db_signal = AISignal(
                            user_id=user.id,
                            symbol=symbol,
                            signal=signal.get("signal", "HOLD"),
                            confidence=signal.get("confidence", 0),
                            target_price=signal.get("target_price"),
                            stop_loss=signal.get("stop_loss"),
                            reasoning=signal.get("reasoning", ""),
                            model_used=signal.get("model", "unknown")
                        )
                        db.add(db_signal)
                        db.commit()

                        if signal.get("signal") in ("BUY", "SELL"):
                            auto_result = engine.auto_trade(signal, quote["current"])
                            if auto_result.get("success"):
                                logger.info(f"Auto-trade for {user.username} - {symbol}: {auto_result}")
                                await broadcast({"type": "auto_trade", "user": user.username, "symbol": symbol, "result": auto_result})
                    except Exception as inner_e:
                        logger.error(f"Error auto-trading {symbol} for {user.username}: {inner_e}")

        except Exception as e:
            logger.error(f"Background auto-trade loop error: {e}")

        await asyncio.sleep(3600)


async def background_social_sentiment_scan():
    """
    Social sentiment scan: runs every 30 minutes.
    Detects extreme retail sentiment on StockTwits (free public API, no auth).
    When a stock hits extreme bullish/bearish readings, logs an alert and
    broadcasts to WebSocket clients so the dashboard can show a warning.
    Also detects active macro scenarios (2028 GIC, etc.) and broadcasts alerts.
    """
    await asyncio.sleep(120)  # Wait 2 min after startup
    while True:
        try:
            db = next(get_db())
            users = db.query(User).all()

            # Collect all symbols across all user watchlists
            all_symbols = set(md.DEFAULT_WATCHLIST)
            for user in users:
                wl = get_setting(db, "watchlist", user.id, "[]")
                try:
                    all_symbols.update(json.loads(wl))
                except:
                    pass
            watchlist = list(all_symbols)

            # Scan for extreme StockTwits sentiment
            alerts = ss.scan_sentiment_alerts(watchlist)
            if alerts:
                for sym, data in alerts.items():
                    label = data.get("sentiment_label", "NEUTRAL")
                    score = data.get("sentiment_score", 0)
                    logger.info(f"[SocialScan] ALERT: {sym} is {label} ({score:+.2f}) on StockTwits")
                    await broadcast({
                        "type": "social_sentiment_alert",
                        "symbol": sym,
                        "sentiment": label,
                        "score": score,
                        "bullish": data.get("bullish", 0),
                        "bearish": data.get("bearish", 0),
                        "total": data.get("total_messages", 0),
                    })

            # Check for active macro scenarios
            active_macros = ni.detect_active_macro_scenarios(hours_back=4)
            if active_macros:
                for scenario in active_macros:
                    logger.warning(f"[MacroAlert] ACTIVE SCENARIO: {scenario['name']} (severity: {scenario['severity']})")
                    await broadcast({
                        "type": "macro_scenario_alert",
                        "scenario": scenario["name"],
                        "severity": scenario["severity"],
                        "stocks_to_avoid": scenario["stocks_to_avoid"],
                        "beneficiaries": scenario["potential_beneficiaries"],
                        "evidence_count": len(scenario["evidence"]),
                    })

        except Exception as e:
            logger.error(f"[SocialScan] Loop error: {e}")

        await asyncio.sleep(1800)  # Run every 30 minutes


async def background_blog_scan():
    """
    Official blog monitor: runs every 15 minutes.
    Scans Anthropic, OpenAI, Google DeepMind, Meta AI, Microsoft AI, AWS ML blogs
    via RSS feeds for competitive disruption signals.

    This is the EARLIEST signal layer — official blog posts often appear
    hours before financial news covers the same story (e.g. the Claude Code COBOL
    post that caused IBM -13% appeared on Anthropic's blog before any news article).

    When a HIGH/CRITICAL impact post is detected:
    1. Logs an alert with affected stocks
    2. Broadcasts WebSocket event to frontend
    3. Triggers immediate AI re-analysis for affected watchlist stocks
    """
    await asyncio.sleep(30)  # Short delay after startup (priority task)
    last_seen_links: set = set()  # Avoid re-processing same post

    while True:
        try:
            alerts = bm.scan_all_blogs(hours_back=16)

            new_alerts = [a for a in alerts if a.get("link") not in last_seen_links]
            if not new_alerts:
                await asyncio.sleep(900)  # 15 min
                continue

            db = next(get_db())
            users = db.query(User).all()
            affected = bm.get_affected_symbols(new_alerts)

            for alert in new_alerts:
                last_seen_links.add(alert.get("link", ""))
                # Broadcast to frontend dashboard immediately
                await broadcast({
                    "type": "blog_alert",
                    "source": alert["source_name"],
                    "title": alert["title"],
                    "link": alert["link"],
                    "published": alert["published"],
                    "severity": alert["max_severity"],
                    "sell_stocks": [s for imp in alert["impacts"] for s in imp["stocks_to_avoid"]],
                    "watch_stocks": [s for imp in alert["impacts"] for s in imp["stocks_to_watch"]],
                    "reason": alert["impacts"][0]["reason"] if alert["impacts"] else "",
                })
                logger.warning(
                    f"[BlogMonitor] NEW ALERT [{alert['max_severity']}] {alert['source_name']}: "
                    f"\"{alert['title']}\" → SELL: {affected['sell']} | WATCH: {affected['watch']}"
                )

            # Trigger immediate AI re-analysis for affected stocks (high/critical only)
            high_alerts = [a for a in new_alerts if a["max_severity"] in ("HIGH", "CRITICAL")]
            if high_alerts and affected["sell"]:
                blog_context = bm.build_blog_alert_context(high_alerts)

                for user in users:
                    auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                    if not auto_trade_enabled:
                        continue

                    api_key = get_setting(db, "deepseek_api_key", user.id, "")
                    ai_provider = get_setting(db, "ai_provider", user.id, "deepseek_api")
                    watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                    watchlist = json.loads(watchlist_json)

                    # Only re-analyze stocks that are in our watchlist AND affected
                    urgent_symbols = [s for s in affected["sell"] if s in watchlist]
                    if not urgent_symbols:
                        continue

                    engine = TradingEngine(db, user.id)
                    portfolio_context = build_rich_portfolio_context(db, user.id, engine)

                    logger.info(f"[BlogMonitor] Urgent re-analysis for: {urgent_symbols}")
                    for symbol in urgent_symbols:
                        try:
                            await asyncio.sleep(1)
                            quote = price_cache.get(symbol) or md.get_stock_quote(symbol)
                            if not quote:
                                continue
                            history = md.get_stock_history(symbol, period="1mo")
                            indicators = md.get_technical_indicators(symbol)
                            news = md.get_stock_news(symbol)

                            signal = ai.analyze_stock(
                                ai_provider, api_key, symbol, quote,
                                indicators, history, news,
                                portfolio_context, blog_context
                            )

                            db_signal = AISignal(
                                user_id=user.id,
                                symbol=symbol,
                                signal=signal.get("signal", "HOLD"),
                                confidence=signal.get("confidence", 0),
                                target_price=signal.get("target_price"),
                                stop_loss=signal.get("stop_loss"),
                                reasoning=f"[BLOG-ALERT] {signal.get('reasoning', '')}",
                                model_used=signal.get("model", "unknown")
                            )
                            db.add(db_signal)
                            db.commit()

                            if signal.get("signal") in ("SELL", "COVER"):
                                auto_result = engine.auto_trade(signal, quote["current"])
                                if auto_result.get("success"):
                                    logger.info(f"[BlogMonitor] Blog-triggered trade: {symbol} → {auto_result}")
                                    await broadcast({
                                        "type": "auto_trade",
                                        "user": user.username,
                                        "symbol": symbol,
                                        "result": auto_result,
                                        "trigger": "blog_alert",
                                        "blog_title": high_alerts[0]["title"],
                                    })
                        except Exception as e:
                            logger.error(f"[BlogMonitor] Error re-analyzing {symbol}: {e}")

        except Exception as e:
            logger.error(f"[BlogMonitor] Loop error: {e}")

        await asyncio.sleep(900)  # Run every 15 minutes


async def background_event_scan():
    """
    Pre-event scan: runs every 20 minutes.
    Identifies stocks with imminent events (earnings, FOMC, CPI) within 48 hours
    and triggers an immediate AI analysis so we can position BEFORE the announcement.
    """
    await asyncio.sleep(60)  # Wait 1 min after startup before first scan
    while True:
        try:
            db = next(get_db())
            users = db.query(User).all()

            for user in users:
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                api_key = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "deepseek_api")
                watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                watchlist = json.loads(watchlist_json)

                # Find symbols with imminent events in the next 2 days
                priority_symbols = em.get_event_priority_symbols(watchlist, days_ahead=2)
                if not priority_symbols:
                    continue

                logger.info(f"[EventScan] Imminent events detected for: {priority_symbols}")

                engine = TradingEngine(db, user.id)
                portfolio_context = build_rich_portfolio_context(db, user.id, engine)
                event_context = em.build_event_context(watchlist, days_ahead=3)

                for symbol in priority_symbols:
                    try:
                        await asyncio.sleep(1)
                        quote = price_cache.get(symbol) or md.get_stock_quote(symbol)
                        if not quote:
                            continue

                        history = md.get_stock_history(symbol, period="3mo")
                        indicators = md.get_technical_indicators(symbol)
                        news = md.get_stock_news(symbol)

                        signal = ai.analyze_stock(
                            ai_provider, api_key, symbol, quote,
                            indicators, history, news,
                            portfolio_context, event_context
                        )

                        db_signal = AISignal(
                            user_id=user.id,
                            symbol=symbol,
                            signal=signal.get("signal", "HOLD"),
                            confidence=signal.get("confidence", 0),
                            target_price=signal.get("target_price"),
                            stop_loss=signal.get("stop_loss"),
                            reasoning=f"[PRE-EVENT] {signal.get('reasoning', '')}",
                            model_used=signal.get("model", "unknown")
                        )
                        db.add(db_signal)
                        db.commit()

                        if signal.get("signal") in ("BUY", "SELL", "COVER"):
                            auto_result = engine.auto_trade(signal, quote["current"])
                            if auto_result.get("success"):
                                logger.info(f"[EventScan] Pre-event trade: {user.username} {symbol} → {auto_result}")
                                await broadcast({"type": "auto_trade", "user": user.username, "symbol": symbol, "result": auto_result, "trigger": "pre_event"})
                    except Exception as e:
                        logger.error(f"[EventScan] Error analyzing {symbol}: {e}")

        except Exception as e:
            logger.error(f"[EventScan] Loop error: {e}")

        await asyncio.sleep(1200)  # Run every 20 minutes


async def background_news_scan():
    """
    Fast news scan: runs every 10 minutes.
    Detects breaking competitive disruption signals (e.g., Anthropic → IBM, BYD → TSLA)
    and immediately triggers AI analysis + trade for affected stocks.
    This is the 'second-order news impact' detector.
    """
    await asyncio.sleep(90)  # Wait 90s after startup
    last_threat_seen = {}   # symbol -> last threat title, to avoid re-trading same news

    while True:
        try:
            db = next(get_db())
            users = db.query(User).all()

            for user in users:
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                api_key = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "deepseek_api")
                watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                watchlist = json.loads(watchlist_json)

                # Scan for new competitive threats (last 2 hours only - fresh news)
                threat_map = ni.scan_all_threats(watchlist, hours_back=2)

                for symbol, threats in threat_map.items():
                    # Skip if we already acted on this exact news
                    new_threats = [
                        t for t in threats
                        if t["news_title"] != last_threat_seen.get(symbol)
                    ]
                    if not new_threats:
                        continue

                    logger.info(f"[NewsScan] BREAKING: {len(new_threats)} new threat(s) for {symbol}")

                    engine = TradingEngine(db, user.id)
                    portfolio_context = build_rich_portfolio_context(db, user.id, engine)

                    quote = price_cache.get(symbol) or md.get_stock_quote(symbol)
                    if not quote:
                        continue

                    history = md.get_stock_history(symbol, period="1mo")
                    indicators = md.get_technical_indicators(symbol)
                    news = md.get_stock_news(symbol)

                    threat_context = ni.build_threat_context(symbol, new_threats)

                    signal = ai.analyze_stock(
                        ai_provider, api_key, symbol, quote,
                        indicators, history, news,
                        portfolio_context, threat_context
                    )

                    rl.record_signal_state(signal, quote, indicators or {}, threat_context, portfolio_context)

                    db_signal = AISignal(
                        user_id=user.id,
                        symbol=symbol,
                        signal=signal.get("signal", "HOLD"),
                        confidence=signal.get("confidence", 0),
                        target_price=signal.get("target_price"),
                        stop_loss=signal.get("stop_loss"),
                        reasoning=f"[BREAKING NEWS] {signal.get('reasoning', '')}",
                        model_used=signal.get("model", "unknown")
                    )
                    db.add(db_signal)
                    db.commit()

                    # Mark this news as seen
                    last_threat_seen[symbol] = new_threats[0]["news_title"]

                    if signal.get("signal") in ("BUY", "SELL", "COVER"):
                        auto_result = engine.auto_trade(signal, quote["current"])
                        if auto_result.get("success"):
                            logger.info(f"[NewsScan] Breaking-news trade: {symbol} → {signal['signal']}")
                            await broadcast({
                                "type": "auto_trade",
                                "user": user.username,
                                "symbol": symbol,
                                "result": auto_result,
                                "trigger": "breaking_news",
                                "threat": new_threats[0]["news_title"]
                            })

                # Also backfill RL outcomes once per day (run at ~midnight UTC)
                if datetime.utcnow().hour == 0 and datetime.utcnow().minute < 10:
                    rl.update_trade_outcomes()

        except Exception as e:
            logger.error(f"[NewsScan] Loop error: {e}")

        await asyncio.sleep(600)  # Every 10 minutes


async def background_daily_summary():
    """
    Task 7 — Daily digest emails: 2 per day.
      • Pre-market:  UTC 14:20 (EST 9:20 AM, NZT 3:20 AM) — 10 min before open
      • Post-market: UTC 21:05 (EST 4:05 PM, NZT 10:05 AM) — 5 min after close
    Aggregates portfolio, today's trades, blog / macro / sentiment alerts → one email.
    """
    await asyncio.sleep(120)  # Wait 2 min after startup before first check

    # Track which emails we've sent today so we don't double-send
    _sent_today = {"pre_market": None, "post_market": None}  # date string -> sent flag

    while True:
        try:
            now = datetime.utcnow()
            today_str = now.strftime("%Y-%m-%d")

            # Reset tracker at UTC midnight
            if _sent_today["pre_market"] and _sent_today["pre_market"] != today_str:
                _sent_today = {"pre_market": None, "post_market": None}

            h, m = now.hour, now.minute

            # Determine if it's time to fire
            fire_type = None
            if h == 14 and 20 <= m <= 29 and _sent_today["pre_market"] != today_str:
                fire_type = "pre_market"
            elif h == 21 and 5 <= m <= 14 and _sent_today["post_market"] != today_str:
                fire_type = "post_market"

            if fire_type:
                try:
                    db = next(get_db())
                    users = db.query(User).all()

                    for user in users:
                        if get_setting(db, "notify_enabled", user.id, "false") != "true":
                            continue

                        engine = TradingEngine(db, user.id)
                        portfolio = engine.get_portfolio_summary()

                        # Today's trades from DB
                        from sqlalchemy import func
                        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                        db_trades = db.query(Trade).filter(
                            Trade.user_id == user.id,
                            Trade.timestamp >= today_start
                        ).order_by(Trade.timestamp.desc()).all()
                        trades_today = [
                            {
                                "symbol": t.symbol,
                                "side": t.side,
                                "quantity": t.quantity,
                                "price": t.price,
                                "total": t.total_value,
                                "reasoning": t.reasoning or "",
                                "trigger": t.trigger if hasattr(t, "trigger") else "auto",
                            }
                            for t in db_trades
                        ]

                        # Gather current blog / macro / sentiment alerts
                        watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                        watchlist = json.loads(watchlist_json)

                        loop = asyncio.get_event_loop()
                        blog_alerts = await loop.run_in_executor(None, lambda: bm.scan_all_blogs(hours_back=12))
                        macro_alerts = await loop.run_in_executor(None, lambda: ni.detect_active_macro_scenarios(hours_back=12))
                        sentiment_alerts = await loop.run_in_executor(
                            None, lambda: ss.scan_sentiment_alerts(watchlist)
                        )

                        await loop.run_in_executor(
                            None,
                            lambda: notifier.notify_daily_summary(
                                db, fire_type, portfolio,
                                trades_today, blog_alerts, macro_alerts, sentiment_alerts
                            )
                        )
                        logger.info(f"[DailySummary] Sent {fire_type} digest for user {user.username}")

                    _sent_today[fire_type] = today_str

                except Exception as e:
                    logger.error(f"[DailySummary] Error sending {fire_type} digest: {e}")

        except Exception as e:
            logger.error(f"[DailySummary] Loop error: {e}")

        await asyncio.sleep(60)  # Check every minute


async def broadcast(data: dict):
    """Broadcast message to all connected WebSocket clients."""
    dead = []
    message = json.dumps(data)
    for ws in active_connections:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in active_connections:
            active_connections.remove(ws)

# ─────────────────────────────────────────────
# Auth Endpoints
# ─────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(user_data: UserRegister, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    new_user = User(
        username=user_data.username,
        hashed_password=get_password_hash(user_data.password),
        email=user_data.email,
        balance=0.0  # Start with zero balance, needs recharge
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # Initialize default settings for new user
    defaults = {
        "auto_trade_enabled": "false",
        "auto_trade_min_confidence": "0.75",
        "risk_per_trade_pct": "2.0",
        "ai_provider": "deepseek_api",
        "watchlist": json.dumps(md.DEFAULT_WATCHLIST),
        "refresh_interval_seconds": "30",
        "initial_cash": "0.0",
    }
    for key, val in defaults.items():
        set_setting(db, key, val, new_user.id)
        
    return {"message": "User registered successfully"}

@app.post("/api/auth/login")
async def login(user_data: UserLogin, db: Session = Depends(get_db)):
    if user_data.username == "admin" and user_data.password == "admin":
        user = db.query(User).filter(User.username == "admin").first()
        if not user:
            user = User(
                username="admin",
                hashed_password=get_password_hash("admin"),
                email="admin@example.com",
                balance=100000.0
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            
            defaults = {
                "auto_trade_enabled": "true",
                "auto_trade_min_confidence": "0.75",
                "risk_per_trade_pct": "2.0",
                "ai_provider": "deepseek_api",
                "watchlist": json.dumps(md.DEFAULT_WATCHLIST),
                "refresh_interval_seconds": "30",
                "initial_cash": "100000.0",
            }
            for key, val in defaults.items():
                set_setting(db, key, val, user.id)
        else:
            set_setting(db, "auto_trade_enabled", "true", user.id)
    else:
        user = db.query(User).filter(User.username == user_data.username).first()
        if not user or not verify_password(user_data.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid username or password")
    
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/auth/auto-login")
async def auto_login(db: Session = Depends(get_db)):
    """Auto-login as default trader user without requiring credentials."""
    user = db.query(User).filter(User.username == "trader").first()
    if not user:
        user = User(
            username="trader",
            hashed_password=get_password_hash("trader"),
            email="trader@localhost",
            balance=100000.0
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        defaults = {
            "auto_trade_enabled": "false",
            "auto_trade_min_confidence": "0.75",
            "risk_per_trade_pct": "2.0",
            "ai_provider": "deepseek_api",
            "watchlist": json.dumps(md.DEFAULT_WATCHLIST),
            "refresh_interval_seconds": "30",
            "initial_cash": "100000.0",
        }
        for key, val in defaults.items():
            set_setting(db, key, val, user.id)
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/auth/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "balance": current_user.balance
    }

@app.post("/api/transfer")
async def transfer_funds(request: TransferRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if request.type == "DEPOSIT":
        current_user.balance += request.amount
    elif request.type == "WITHDRAW":
        if current_user.balance < request.amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        current_user.balance -= request.amount
    else:
        raise HTTPException(status_code=400, detail="Invalid transfer type")
    
    db.commit()
    return {"balance": current_user.balance}


# ─────────────────────────────────────────────
# REST API Endpoints
# ─────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))


@app.get("/api/markets")
async def get_markets():
    """Get all global market indices."""
    global market_cache, last_market_fetch
    now = datetime.utcnow()
    if not market_cache or last_market_fetch is None or (now - last_market_fetch).seconds > 300:
        try:
            market_cache = md.get_all_indices()
            last_market_fetch = now
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"data": market_cache, "timestamp": now.isoformat()}


import asyncio

@app.get("/api/stock/{symbol}")
async def get_stock(symbol: str, period: str = "3mo"):
    """Get full data for a single stock."""
    symbol = symbol.upper()
    loop = asyncio.get_event_loop()
    
    quote, history, indicators, news = await asyncio.gather(
        loop.run_in_executor(None, md.get_stock_quote, symbol),
        loop.run_in_executor(None, lambda: md.get_stock_history(symbol, period=period)),
        loop.run_in_executor(None, md.get_technical_indicators, symbol),
        loop.run_in_executor(None, md.get_stock_news, symbol),
    )
    
    if not quote:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")
        
    return {
        "quote": quote,
        "history": history,
        "indicators": indicators,
        "news": news,
    }


@app.get("/api/stock/{symbol}/history")
async def get_stock_history(symbol: str, period: str = "3mo", interval: str = "1d"):
    """Get OHLCV historical data."""
    symbol = symbol.upper()
    history = md.get_stock_history(symbol, period=period, interval=interval)
    return {"symbol": symbol, "period": period, "interval": interval, "data": history}


@app.get("/api/portfolio")
async def get_portfolio(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get portfolio summary and positions."""
    engine = TradingEngine(db, current_user.id)
    return engine.get_portfolio_summary()


@app.get("/api/trades")
async def get_trades(limit: int = 50, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get trade history."""
    trades = db.query(Trade).filter(Trade.user_id == current_user.id).order_by(Trade.timestamp.desc()).limit(limit).all()
    return {"trades": [
        {
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "price": t.price,
            "total_value": t.total_value,
            "ai_triggered": t.ai_triggered,
            "ai_confidence": t.ai_confidence,
            "reasoning": t.reasoning,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
        }
        for t in trades
    ]}


@app.post("/api/trade")
async def execute_trade(request: TradeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Execute a manual trade."""
    engine = TradingEngine(db, current_user.id)
    price = request.price
    if price is None:
        quote = md.get_stock_quote(request.symbol.upper())
        if not quote:
            raise HTTPException(status_code=404, detail="Cannot fetch live price")
        price = quote["current"]

    if request.side.upper() == "BUY":
        result = engine.execute_buy(request.symbol.upper(), request.quantity, price)
    elif request.side.upper() == "SELL":
        result = engine.execute_sell(request.symbol.upper(), request.quantity, price)
    else:
        raise HTTPException(status_code=400, detail="Side must be BUY or SELL")

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Trade failed"))

    await broadcast({"type": "trade_executed", "user": current_user.username, "trade": result.get("trade")})
    return result


@app.post("/api/analyze")
async def analyze_stock(request: AnalyzeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Run DeepSeek-R1 analysis on a stock."""
    symbol = request.symbol.upper()
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    ai_provider = get_setting(db, "ai_provider", current_user.id, "deepseek_api")

    quote = md.get_stock_quote(symbol)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    history = md.get_stock_history(symbol, period="6mo")
    indicators = md.get_technical_indicators(symbol)
    news = md.get_stock_news(symbol)

    # Portfolio context
    engine = TradingEngine(db, current_user.id)
    summary = engine.get_portfolio_summary()
    portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"

    signal = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context)

    # Store signal in DB
    db_signal = AISignal(
        user_id=current_user.id,
        symbol=symbol,
        signal=signal.get("signal", "HOLD"),
        confidence=signal.get("confidence", 0),
        target_price=signal.get("target_price"),
        stop_loss=signal.get("stop_loss"),
        reasoning=signal.get("reasoning", ""),
    )
    db.add(db_signal)
    db.commit()

    # Auto-trade if enabled
    auto_result = None
    if signal.get("signal") in ("BUY", "SELL", "SHORT", "COVER"):
        auto_result = engine.auto_trade(signal, quote["current"])
        if auto_result.get("success"):
            await broadcast({"type": "auto_trade", "user": current_user.username, "signal": signal, "trade": auto_result})

    return {"signal": signal, "quote": quote, "auto_trade": auto_result}


@app.post("/api/analyze-portfolio")
async def analyze_portfolio(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Run DeepSeek-R1 portfolio analysis."""
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    ai_provider = get_setting(db, "ai_provider", current_user.id, "deepseek_api")
    engine = TradingEngine(db, current_user.id)
    summary = engine.get_portfolio_summary()
    market_summary = {}
    if market_cache:
        for region, indices in market_cache.items():
            market_summary[region] = [
                {"name": idx.get("name"), "change_pct": idx.get("change_pct")}
                for idx in indices[:3]
            ]
    result = ai.analyze_portfolio(ai_provider, api_key, summary["positions"], market_summary)
    return result


@app.post("/api/chat")
async def chat(request: ChatRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Chat with DeepSeek-R1 about markets."""
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    ai_provider = get_setting(db, "ai_provider", current_user.id, "deepseek_api")
    engine = TradingEngine(db, current_user.id)
    summary = engine.get_portfolio_summary()
    context = f"Portfolio equity: ${summary['total_equity']:,.2f}"
    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    response = ai.chat_with_ai(ai_provider, api_key, messages, context)
    return {"response": response}


@app.get("/api/signals")
async def get_signals(limit: int = 20, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get recent AI signals."""
    signals = db.query(AISignal).filter(AISignal.user_id == current_user.id).order_by(AISignal.timestamp.desc()).limit(limit).all()
    return {"signals": [
        {
            "id": s.id,
            "symbol": s.symbol,
            "signal": s.signal,
            "confidence": s.confidence,
            "target_price": s.target_price,
            "stop_loss": s.stop_loss,
            "reasoning": s.reasoning,
            "model": s.model_used,
            "timestamp": s.timestamp.isoformat() if s.timestamp else None,
        }
        for s in signals
    ]}


@app.get("/api/watchlist")
async def get_watchlist(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current watchlist."""
    watchlist_json = get_setting(db, "watchlist", current_user.id, json.dumps(md.DEFAULT_WATCHLIST))
    return {"symbols": json.loads(watchlist_json)}


@app.post("/api/watchlist")
async def update_watchlist(item: WatchlistUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Add or remove from watchlist."""
    symbol = item.symbol.upper()
    watchlist_json = get_setting(db, "watchlist", current_user.id, json.dumps(md.DEFAULT_WATCHLIST))
    watchlist = set(json.loads(watchlist_json))
    if item.action == "add":
        watchlist.add(symbol)
    elif item.action == "remove":
        watchlist.discard(symbol)
    
    set_setting(db, "watchlist", json.dumps(list(watchlist)), current_user.id)
    return {"watchlist": list(watchlist)}

# ─────────────────────────────────────────────
# OpenClaw Integration
# ─────────────────────────────────────────────

@app.post("/api/openclaw/webhook")
async def openclaw_webhook(request: OpenClawWebhook, db: Session = Depends(get_db)):
    """Endpoint for OpenClaw Skill to query portfolio or analyze stocks remotely."""
    
    # Allow messages from both DMs and group chats seamlessly
    # The user requested to invite the AI into a group to avoid using their personal number.
    pass
        
    command = request.command.lower().strip()
    
    # 2. Isolation Strategy 1: Command Prefix Checking
    if not command.startswith("/") and command not in ["portfolio", "balance", "status", "analyze"]:
        # Drop all normal conversational chatter
        return {"response": ""}

    try:
        if command in ["/portfolio", "portfolio", "balance", "status"]:
            engine = TradingEngine(db)
            summary = engine.get_portfolio_summary()
            
            msg = f"💼 **AlphaTrader Portfolio ({summary['provider']})**\n\n"
            msg += f"Total Equity: ${summary['total_equity']:,.2f}\n"
            msg += f"Cash Balance: ${summary['cash']:,.2f}\n"
            pnl_sign = "+" if summary['total_return'] >= 0 else ""
            msg += f"Total Return: {pnl_sign}${summary['total_return']:,.2f} ({summary['total_return_pct']:.2f}%)\n\n"
            
            if summary['positions']:
                msg += "📈 **Top Open Positions:**\n"
                # Sort by weight or market value
                sorted_pos = sorted(summary['positions'], key=lambda x: x['market_value'], reverse=True)[:5]
                for p in sorted_pos:
                    upnl_sign = "+" if p['unrealized_pnl'] >= 0 else ""
                    msg += f"- {p['symbol']}: {p['quantity']} shares @ ${p['current_price']} ({upnl_sign}${p['unrealized_pnl']:,.2f})\n"
            else:
                msg += "No open positions."
                
            return {"response": msg}
            
        elif command in ["/analyze", "analyze"] and request.symbol:
            symbol = request.symbol.upper()
            quote = md.get_stock_quote(symbol)
            if not quote:
                return {"response": f"❌ Error: Could not fetch real-time data for {symbol}"}
                
            indicators = md.get_technical_indicators(symbol)
            history = md.get_stock_history(symbol, period="3mo")
            news = md.get_stock_news(symbol)
            
            api_key = get_setting(db, "deepseek_api_key", "")
            ai_provider = get_setting(db, "ai_provider", "deepseek_api")
            engine = TradingEngine(db)
            summary = engine.get_portfolio_summary()
            portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"
            
            import deepseek_ai as ai
            signal_data = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context)
            
            sig = signal_data.get("signal", "HOLD")
            conf = signal_data.get("confidence", 0) * 100
            reasoning = signal_data.get("reasoning", "")
            
            emoji = "📈" if sig == "BUY" else "📉" if sig == "SELL" else "⏸️"
            msg = f"{emoji} **DeepSeek-R1 Analysis: {symbol}**\n"
            msg += f"**Signal:** {sig} ({conf:.0f}% confidence)\n"
            msg += f"**Current Price:** ${quote['current']}\n\n"
            msg += f"**Reasoning:**\n{reasoning}\n\n"
            
            target = signal_data.get("target_price")
            stop = signal_data.get("stop_loss")
            if target: msg += f"🎯 Target: ${target}\n"
            if stop: msg += f"🛡️ Stop Loss: ${stop}\n"
            
            return {"response": msg}
            
        else:
            return {"response": "Unknown command. Use '/portfolio' or '/analyze AAPL'."}
            
    except Exception as e:
        logger.error(f"OpenClaw webhook error: {e}")
        return {"response": f"⚠️ AlphaTrader Error: {str(e)}"}


@app.get("/api/settings")
async def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get all settings (API key is masked)."""
    keys = [
        "auto_trade_enabled", "auto_trade_min_confidence",
        "risk_per_trade_pct", "refresh_interval_seconds", "ai_provider",
        "alpaca_paper_mode"
    ]
    result = {}
    for key in keys:
        result[key] = get_setting(db, key, current_user.id, "")
    
    # Mask deepseek api key
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    result["deepseek_api_key_set"] = bool(api_key)
    result["deepseek_api_key_preview"] = f"{api_key[:8]}..." if len(api_key) > 8 else ("" if not api_key else api_key)

    # Mask alpaca keys
    alpaca_key = get_setting(db, "alpaca_api_key", current_user.id, "")
    alpaca_secret = get_setting(db, "alpaca_secret_key", current_user.id, "")
    result["alpaca_api_key_set"] = bool(alpaca_key)
    result["alpaca_secret_key_set"] = bool(alpaca_secret)
    result["alpaca_api_key_preview"] = f"{alpaca_key[:8]}..." if len(alpaca_key) > 8 else ("" if not alpaca_key else alpaca_key)
    
    return result


@app.post("/api/settings")
async def update_setting(update: SettingsUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Update a setting."""
    set_setting(db, update.key, update.value, current_user.id)
    return {"key": update.key, "updated": True}


@app.post("/api/reset-portfolio")
async def reset_portfolio(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Reset paper trading portfolio to initial state."""
    from database import Position
    db.query(Trade).filter(Trade.user_id == current_user.id).delete()
    db.query(Position).filter(Position.user_id == current_user.id).delete()
    db.query(AISignal).filter(AISignal.user_id == current_user.id).delete()
    db.commit()
    current_user.balance = 100000.0
    db.commit()
    return {"success": True, "message": "Portfolio reset to $100,000"}


@app.get("/api/search")
async def search_stocks(q: str):
    """Search for stocks by symbol."""
    results = md.search_stocks(q)
    return {"results": results}


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    logger.info(f"WebSocket client connected. Total: {len(active_connections)}")
    try:
        # Send initial data
        if price_cache:
            await websocket.send_text(json.dumps({
                "type": "price_update",
                "prices": {k: v["current"] for k, v in price_cache.items()},
                "timestamp": datetime.utcnow().isoformat()
            }))
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total: {len(active_connections)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
