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
from datetime import datetime, timedelta
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
import email_reporter as er
import layoff_event_framework as lef
import rl_data_collector as rl
import social_sentiment as ss
import blog_monitor as bm
import kronos_analysis as ka
import notifier
import cot_data as cot
import position_sizer as ps
import global_context as gc
import scenario_tracker as st
from trading_engine import TradingEngine
from database import create_tables, get_db, get_setting, set_setting, Trade, AISignal, WatchedStock, Settings, User, PendingTrade, SignalArchive
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
# Geo scan cooldown: symbol -> YYYY-MM-DD of last successful geo-triggered trade
_geo_traded_today: Dict = {}


def _is_stop_loss_cooldown(symbol: str, user_id: int, db) -> bool:
    """Return True if symbol had a [STOP-LOSS] sell within the last 3 days."""
    cutoff = datetime.utcnow() - timedelta(days=3)
    recent = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.symbol == symbol,
            Trade.side == "SELL",
            Trade.timestamp >= cutoff,
            Trade.reasoning.like("%[STOP-LOSS]%"),
        )
        .first()
    )
    return recent is not None


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
    task8 = asyncio.create_task(background_pending_trade_executor())
    task9 = asyncio.create_task(background_email_reporter())
    task10 = asyncio.create_task(background_email_reply_checker())
    task11 = asyncio.create_task(background_stop_loss_monitor())
    task12 = asyncio.create_task(background_global_market_scan())
    logger.info("Background tasks started: price_refresh + auto_trade_loop + event_scan + news_scan + social_sentiment + blog_monitor + kronos_gpu + daily_digest + pending_trade_executor + email_reporter + email_reply_checker + stop_loss_monitor + global_market_scan")
    yield
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()
    task5.cancel()
    task6.cancel()
    task7.cancel()
    task8.cancel()
    task9.cancel()
    task10.cancel()
    task11.cancel()
    task12.cancel()
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

class LayoffEventInput(BaseModel):
    symbol: str
    announcement_date: str  # YYYY-MM-DD
    layoff_percentage: Optional[float] = None
    layoff_employees: Optional[int] = None
    guidance_change: Optional[str] = None  # up/down/none

class LayoffFrameworkRequest(BaseModel):
    events: List[LayoffEventInput]
    benchmark_symbol: str = "SPY"
    lookahead_days: int = 20

class LayoffDiscoveryRequest(BaseModel):
    symbols: Optional[List[str]] = None
    use_watchlist: bool = True
    hours_back: int = 168
    max_items: int = 50
    
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


def _next_trading_day_utc(now: datetime) -> datetime:
    """Return next weekday date at 00:00 UTC (simple Mon-Fri calendar)."""
    next_day = now.date()
    while True:
        next_day = next_day.replace(day=next_day.day)  # no-op, keep date object
        next_day = next_day + timedelta(days=1)
        if next_day.weekday() < 5:  # Mon-Fri
            break
    return datetime.combine(next_day, datetime.min.time())


def _within_market_open_window(now: datetime) -> bool:
    """Return True if US or China A-share market is open (UTC times, Mon-Fri only)."""
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    total = now.hour * 60 + now.minute
    # US NYSE/NASDAQ: 09:30-16:00 EST = 14:30-21:00 UTC
    us_open = 14 * 60 + 30 <= total <= 21 * 60
    # China A-share morning session: 09:30-11:30 CST = 01:30-03:30 UTC
    cn_morning = 1 * 60 + 30 <= total <= 3 * 60 + 30
    # China A-share afternoon session: 13:00-15:00 CST = 05:00-07:00 UTC
    cn_afternoon = 5 * 60 <= total <= 7 * 60
    return us_open or cn_morning or cn_afternoon


def _schedule_next_day_buy(db, user_id: int, symbol: str, reason: str, source_title: str, trigger: str):
    execute_on = _next_trading_day_utc(datetime.utcnow())
    existing = db.query(PendingTrade).filter(
        PendingTrade.user_id == user_id,
        PendingTrade.symbol == symbol,
        PendingTrade.execute_on == execute_on,
        PendingTrade.trigger == trigger,
        PendingTrade.status == "PENDING",
    ).first()
    if existing:
        return False

    pending = PendingTrade(
        user_id=user_id,
        symbol=symbol,
        side="BUY",
        trigger=trigger,
        reason=reason,
        source_title=source_title,
        execute_on=execute_on,
        status="PENDING",
    )
    db.add(pending)
    db.commit()
    return True


def get_rl_lessons() -> str:
    """Read the latest RL intelligence attribution report and format it for the AI prompt."""
    report_path = "/data/qbao775/AlphaTrader/intelligence_attribution_report.json"
    if not os.path.exists(report_path):
        return ""
    try:
        with open(report_path, "r") as f:
            report = json.load(f)
        
        # Summary of best/worst macro scenarios
        macro_stats = report.get("catalyst_performance", {})
        sector_stats = report.get("sector_performance", {})
        
        lines = ["### Intelligence Performance Attribution (Actual Market Results)"]
        
        # 1. Catalyst Performance
        if macro_stats:
            sorted_macros = sorted(macro_stats.items(), key=lambda x: x[1].get("avg_reward", 0), reverse=True)
            top = [f"{m}: {s.get('avg_reward', 0):+.2f}% avg reward ({s.get('count', 0)} signals)" for m, s in sorted_macros[:3] if s.get('count', 0) > 0]
            bottom = [f"{m}: {s.get('avg_reward', 0):+.2f}% avg reward ({s.get('count', 0)} signals)" for m, s in sorted_macros[-3:] if s.get('count', 0) > 0]
            
            if top:
                lines.append("Most Accurate Catalysts Recently:")
                lines.extend([f"  - {t}" for t in top])
            if bottom:
                lines.append("Least Accurate/Overpriced Catalysts Recently:")
                lines.extend([f"  - {b}" for b in bottom])

        # 2. Sector Performance (Grounding)
        if sector_stats:
            lines.append("\nRecent Sector Success Rates:")
            sorted_sectors = sorted(sector_stats.items(), key=lambda x: x[1].get("avg_reward", 0), reverse=True)
            for sector, data in sorted_sectors:
                if data.get("count", 0) > 0:
                    lines.append(f"  - {sector}: {data['avg_reward']:+.2f}% avg 1d return")

        lines.append("\nINSTRUCTION: Favor signals backed by 'Accurate' catalysts and strong sector trends. Be skeptical of 'Risky' or overextended areas.")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error reading RL lessons: {e}")
        return ""


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

            # Fetch prices in executor (non-blocking yfinance calls) - staggered to avoid rate limits
            loop = asyncio.get_event_loop()
            new_prices = {}
            for sym in all_symbols[:20]:
                try:
                    q = await loop.run_in_executor(None, md.get_stock_quote, sym)
                    if q:
                        new_prices[sym] = q["current"]
                        price_cache[sym] = q
                except Exception as e:
                    logger.error(f"Error fetching {sym}: {e}")
                await asyncio.sleep(1.5)  # Stagger requests to avoid Yahoo Finance rate limit

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

        await asyncio.sleep(120)  # Refresh every 2 min to avoid Yahoo Finance rate limits


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
                ai_provider = get_setting(db, "ai_provider", user.id, "ollama")
                watchlist_json = get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST))
                watchlist = json.loads(watchlist_json)

                engine = TradingEngine(db, user.id)

                # ── Sync local DB positions with Alpaca before making any decision ──
                if engine.use_alpaca:
                    n = engine.sync_positions_from_alpaca()
                    logger.info(f"[AutoTrade] Synced {n} positions from Alpaca for {user.username}")

                # ── Market Regime Filter: skip BUY signals when SPY is below its 20-day MA ──
                spy_bear_market = False
                try:
                    spy_indicators = await loop.run_in_executor(None, md.get_technical_indicators, "SPY")
                    if spy_indicators:
                        spy_price = (price_cache.get("SPY") or {}).get("current", 0)
                        spy_ma20  = spy_indicators.get("ma20", 0)
                        if spy_price and spy_ma20 and spy_price < spy_ma20:
                            spy_bear_market = True
                            logger.warning(
                                f"[AutoTrade] BEAR MARKET filter active — SPY ${spy_price:.2f} < MA20 ${spy_ma20:.2f}. "
                                f"All BUY signals will be suppressed this cycle."
                            )
                except Exception as _e:
                    logger.debug(f"[AutoTrade] SPY trend check failed: {_e}")

                portfolio_context = await loop.run_in_executor(
                    None, build_rich_portfolio_context, db, user.id, engine
                )

                # Run all slow blocking I/O in executor so event loop stays free for HTTP requests
                event_context = await loop.run_in_executor(None, lambda: em.build_event_context(watchlist, days_ahead=7))
                threat_map = await loop.run_in_executor(None, lambda: ni.scan_all_threats(watchlist, hours_back=24))
                active_macros = await loop.run_in_executor(None, lambda: ni.detect_active_macro_scenarios(hours_back=6))
                macro_context = ni.build_macro_scenario_context(active_macros)
                blog_alerts = await loop.run_in_executor(None, lambda: bm.scan_all_blogs(hours_back=12))

                # ── Build global market context once per cycle (5-min TTL cached) ──
                try:
                    global_ctx = await loop.run_in_executor(None, gc.build_global_context)
                    logger.info(f"[AutoTrade] Global context: {gc.get_global_context_summary(global_ctx)}")
                except Exception as _gce:
                    logger.warning(f"[AutoTrade] Global context build failed: {_gce}")
                    global_ctx = None

                rl_lessons = get_rl_lessons()
                for symbol in watchlist:
                    try:
                        await asyncio.sleep(1)  # yield to event loop between symbols
                        quote = price_cache.get(symbol)  # use cache only; price_refresh handles fetching
                        if not quote: continue

                        history, indicators, news = await asyncio.gather(
                            loop.run_in_executor(None, md.get_stock_history, symbol, "6mo"),
                            loop.run_in_executor(None, md.get_technical_indicators, symbol),
                            loop.run_in_executor(None, md.get_stock_news, symbol),
                        )

                        # Kronos K-line forecast (A100 GPU) — already in executor
                        kronos_pred = await loop.run_in_executor(None, ka.predict_next_candles, symbol, history)
                        kronos_context = ka.build_kronos_context(kronos_pred)

                        # COT futures positioning (週報 CFTC data, free, no API key)
                        cot_context = await loop.run_in_executor(None, cot.build_cot_context, symbol)

                        # Kelly Criterion pre-sizing (uses last signal's target/stop if available)
                        # Pull target & stop from the most recent signal for this symbol
                        kelly_context = ""
                        try:
                            last_sig = db.query(AISignal).filter(
                                AISignal.symbol == symbol,
                                AISignal.target_price.isnot(None),
                                AISignal.stop_loss.isnot(None),
                            ).order_by(AISignal.timestamp.desc()).first()
                            if last_sig and quote:
                                engine_tmp = TradingEngine(db, user.id)
                                try:
                                    port_val = float(engine_tmp.alpaca.get_account().equity)
                                except Exception:
                                    port_val = float(get_setting(db, "initial_cash", user.id, "100000"))
                                kelly_sz = ps.kelly_position_size(
                                    confidence=last_sig.confidence or 0.6,
                                    current_price=quote["current"],
                                    target_price=last_sig.target_price,
                                    stop_loss=last_sig.stop_loss,
                                    portfolio_value=port_val,
                                )
                                kelly_context = ps.build_kelly_context(symbol, kelly_sz)
                        except Exception as _ke:
                            logger.debug(f"[Kelly] {symbol} sizing error: {_ke}")

                        # Merge all intelligence layers
                        threats = threat_map.get(symbol, [])
                        threat_context = ni.build_threat_context(symbol, threats)
                        sentiment_context = ss.build_sentiment_context(symbol)
                        blog_context = bm.build_blog_alert_context(blog_alerts, target_symbol=symbol)

                        # ── Fix 2 & 3: Positive catalysts + priority resolution ──
                        catalysts = await loop.run_in_executor(
                            None, ni.detect_catalysts_for_symbol, symbol, 6
                        )
                        catalyst_context = ni.build_catalyst_context(symbol, catalysts)
                        priority_note = ni.resolve_signal_priority(symbol, catalysts, active_macros)

                        full_context = "\n\n".join(filter(None, [
                            event_context, threat_context, catalyst_context,
                            priority_note, macro_context,
                            sentiment_context, blog_context, kronos_context,
                            cot_context, kelly_context,
                        ]))

                        sector = ni.get_symbol_sector(symbol)
                        # AI analysis in executor (Ollama HTTP call — can take 30-60s)
                        signal = await loop.run_in_executor(
                            None, ai.analyze_stock,
                            ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context, full_context, rl_lessons, sector, global_ctx
                        )
                        signal["sector"] = sector

                        # Apply per-market confidence modifier from global context
                        if global_ctx:
                            raw_conf = signal.get("confidence", 0.5)
                            modifier = gc.get_confidence_modifier(global_ctx, symbol)
                            signal["confidence"] = max(0.0, min(1.0, raw_conf * modifier))
                            if modifier != 1.0:
                                logger.debug(f"[AutoTrade] {symbol} confidence {raw_conf:.2f} × {modifier:.2f} = {signal['confidence']:.2f}")

                        # Record to RL training dataset
                        rl.record_signal_state(
                            signal, quote, indicators or {}, 
                            full_context, portfolio_context,
                            catalysts=catalysts,
                            active_macros=active_macros
                        )

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
                            gap_pct = quote.get("change_pct", 0)
                            action  = signal.get("signal")
                            skip_reason = None

                            # Gap Filter: skip BUY if stock already up >3% today
                            if action == "BUY" and gap_pct > 3.0:
                                skip_reason = f"gap filter ({gap_pct:.1f}% up today)"

                            # Bear Market Filter: suppress BUY when SPY < MA20
                            elif action == "BUY" and spy_bear_market:
                                skip_reason = "bear market filter (SPY below MA20)"

                            # Cooldown Filter: skip BUY within 3 days of a losing sell on this symbol
                            elif action == "BUY":
                                cooldown_cutoff = datetime.utcnow() - timedelta(days=3)
                                recent_loss = db.query(Trade).filter(
                                    Trade.user_id == user.id,
                                    Trade.symbol == symbol,
                                    Trade.side == "SELL",
                                    Trade.timestamp >= cooldown_cutoff,
                                ).order_by(Trade.timestamp.desc()).first()
                                if recent_loss:
                                    # Only block if we sold at a loss (sell price < position avg_cost at the time)
                                    # Approximation: check if there's still an open position with lower avg or use reasoning
                                    if recent_loss.reasoning and "[STOP-LOSS]" in recent_loss.reasoning:
                                        skip_reason = f"cooldown: stop-loss triggered on {recent_loss.timestamp.date()}, 3-day ban"

                            if skip_reason:
                                logger.warning(f"[AutoTrade] {symbol} {action} skipped — {skip_reason}")
                            else:
                                auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
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
                    rl_lessons = get_rl_lessons()
                    auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                    if not auto_trade_enabled:
                        continue

                    api_key = get_setting(db, "deepseek_api_key", user.id, "")
                    ai_provider = get_setting(db, "ai_provider", user.id, "ollama")
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
                            quote = price_cache.get(symbol)  # cache-only: price_refresh handles fetching
                            if not quote:
                                continue
                            history = md.get_stock_history(symbol, period="1mo")
                            indicators = md.get_technical_indicators(symbol)
                            news = md.get_stock_news(symbol)

                            signal = ai.analyze_stock(
                                ai_provider, api_key, symbol, quote,
                                indicators, history, news,
                                portfolio_context, blog_context,
                                rl_lessons=rl_lessons,
                                global_context=gc.build_global_context()
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
                                auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
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
            rl_lessons = get_rl_lessons()

            for user in users:
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                api_key = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "ollama")
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
                        quote = price_cache.get(symbol)  # cache-only
                        if not quote:
                            continue

                        history = md.get_stock_history(symbol, period="3mo")
                        indicators = md.get_technical_indicators(symbol)
                        news = md.get_stock_news(symbol)

                        signal = ai.analyze_stock(
                            ai_provider, api_key, symbol, quote,
                            indicators, history, news,
                            portfolio_context, event_context,
                            rl_lessons=rl_lessons,
                            global_context=gc.build_global_context()
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
                            auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
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
                rl_lessons = get_rl_lessons()  # Define at user loop start
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                api_key = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "ollama")
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

                    quote = price_cache.get(symbol)  # cache-only
                    if not quote:
                        continue

                    history = md.get_stock_history(symbol, period="1mo")
                    indicators = md.get_technical_indicators(symbol)
                    news = md.get_stock_news(symbol)

                    threat_context = ni.build_threat_context(symbol, new_threats)

                    # ── Fix 2 & 3: Positive catalysts + priority resolution ──
                    catalysts = ni.detect_catalysts_for_symbol(symbol, hours_back=6)
                    catalyst_context = ni.build_catalyst_context(symbol, catalysts)
                    priority_note = ni.resolve_signal_priority(symbol, catalysts, [])

                    full_context = "\n\n".join(filter(None, [threat_context, catalyst_context, priority_note]))

                    sector = ni.get_symbol_sector(symbol)
                    signal = ai.analyze_stock(
                        ai_provider, api_key, symbol, quote,
                        indicators, history, news,
                        portfolio_context,
                        full_context,
                        rl_lessons=rl_lessons,
                        global_context=gc.build_global_context()
                    )
                    signal["sector"] = sector

                    rl.record_signal_state(
                        signal, quote, indicators or {},
                        full_context,
                        portfolio_context,
                        catalysts=catalysts,
                        active_macros=[]
                    )

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
                        auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
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

                # ── Geopolitical Macro Scan + Auto-Watchlist Expansion ───────────
                # Scan Reuters/BBC/Al Jazeera/White House RSS for breaking global events
                active_macros = ni.detect_active_macro_scenarios(hours_back=3)

                # Auto-expand watchlist based on active scenarios and news keywords
                try:
                    geo_news_raw = ni.fetch_geopolitical_news(hours_back=6)
                    new_tickers, reason = ni.get_watchlist_additions(
                        active_macros, geo_news_raw, watchlist,
                    )
                    if new_tickers:
                        updated_wl = list(set(watchlist) | set(new_tickers))
                        set_setting(db, "watchlist", json.dumps(updated_wl), user.id)
                        watchlist = updated_wl
                        logger.info(f"[AutoWatchlist] 自动加入 {new_tickers} — 原因: {reason}")
                        await broadcast({"type": "watchlist_updated", "added": new_tickers, "reason": reason})
                except Exception as _e:
                    logger.error(f"[AutoWatchlist] Error: {_e}")

                critical_macros = [m for m in active_macros if m["severity"] in ("CRITICAL", "HIGH")]
                if critical_macros:
                    # ── Get current VIX for proportional position scaling (not binary on/off) ──
                    geo_vix = 0.0
                    try:
                        _gctx = gc.build_global_context()
                        geo_vix = _gctx.get("vix", {}).get("value", 0) or 0
                    except Exception:
                        pass

                    for macro in critical_macros:
                        # ── Adaptive scenario health check (replaces rigid "7 day" age gate) ──
                        # Assess actual price performance of beneficiaries since first trade.
                        # The AI will receive this context and decide position size accordingly.
                        scenario_health = st.get_scenario_health(
                            macro.get("name", ""),
                            macro.get("potential_beneficiaries", []),
                            db, user.id, price_cache,
                        )
                        scenario_mult = scenario_health["position_mult"]  # 1.0 / 0.6 / 0.3

                        logger.warning(
                            f"[GeoScan] 🌍 MACRO: {macro['name']} — "
                            f"health={scenario_health['status']} ({scenario_health['avg_pct']:+.1f}%) "
                            f"VIX={geo_vix:.1f} pos_mult={scenario_mult:.1f}"
                        )

                        today_str = datetime.utcnow().strftime("%Y-%m-%d")
                        for _k in list(_geo_traded_today.keys()):
                            if _geo_traded_today[_k] != today_str:
                                del _geo_traded_today[_k]
                        if not _geo_traded_today:
                            _already = db.query(Trade).filter(
                                Trade.user_id == user.id,
                                Trade.timestamp >= today_str,
                                Trade.side == "BUY",
                            ).all()
                            for _t in _already:
                                _geo_traded_today[_t.symbol] = today_str

                        for sym in macro["potential_beneficiaries"]:
                            if sym not in watchlist:
                                continue
                            if _geo_traded_today.get(sym) == today_str:
                                logger.info(f"[GeoScan] {sym} already geo-traded today, skipping")
                                continue
                            quote = price_cache.get(sym) or md.get_stock_quote(sym)
                            if not quote:
                                continue
                            history = md.get_stock_history(sym, period="1mo")
                            indicators = md.get_technical_indicators(sym)
                            news_items = md.get_stock_news(sym)
                            engine = TradingEngine(db, user.id)
                            portfolio_ctx = build_rich_portfolio_context(db, user.id, engine)

                            # Build enriched macro context: scenario health + VIX level
                            base_macro_ctx = ni.build_macro_scenario_context([macro])
                            vix_note = (
                                f"\n### MARKET REGIME\n"
                                f"Current VIX: {geo_vix:.1f} — "
                                f"{'EXTREME FEAR: use very small size' if geo_vix > 35 else 'HIGH FEAR: reduce size' if geo_vix > 25 else 'ELEVATED: moderate caution' if geo_vix > 20 else 'Normal'}\n"
                                f"Position size has been automatically scaled to "
                                f"{ps.vix_position_scale(geo_vix, 1.0) * 100:.0f}% of normal due to VIX.\n"
                                f"Scenario position multiplier: {scenario_mult:.1f}× (based on actual price performance)."
                            )
                            enriched_macro_ctx = base_macro_ctx + "\n" + scenario_health["context_str"] + vix_note

                            # Compute ATR-based stop-loss for this symbol
                            atr = (indicators or {}).get("atr14", 0)
                            current_price = quote.get("current", 0)
                            adaptive_stop = ps.atr_stop_loss(current_price, atr) if current_price > 0 else None

                            sector = ni.get_symbol_sector(sym)
                            signal = ai.analyze_stock(
                                ai_provider, api_key, sym, quote,
                                indicators, history, news_items,
                                portfolio_ctx, enriched_macro_ctx,
                                rl_lessons=rl_lessons,
                                sector=sector,
                                global_context=gc.build_global_context()
                            )
                            signal["sector"] = sector
                            # Inject ATR stop-loss if AI didn't provide one
                            if adaptive_stop and not signal.get("stop_loss"):
                                signal["stop_loss"] = adaptive_stop

                            rl.record_signal_state(
                                signal, quote, indicators or {},
                                enriched_macro_ctx, portfolio_ctx,
                                catalysts=[],
                                active_macros=[macro],
                                sector=sector
                            )

                            db_signal = AISignal(
                                user_id=user.id,
                                symbol=sym,
                                signal=signal.get("signal", "HOLD"),
                                confidence=signal.get("confidence", 0),
                                target_price=signal.get("target_price"),
                                stop_loss=signal.get("stop_loss"),
                                reasoning=f"[GEOPOLITICAL] {macro['name']}: {signal.get('reasoning', '')}",
                                model_used=signal.get("model", "unknown")
                            )
                            db.add(db_signal)
                            db.commit()

                            if signal.get("signal") in ("BUY", "COVER"):
                                # Stop-loss cooldown: 3-day ban after any [STOP-LOSS] sell
                                if _is_stop_loss_cooldown(sym, user.id, db):
                                    logger.warning(f"[GeoScan] {sym} skipped — stop-loss cooldown active (3-day ban)")
                                    continue

                                # Gap Filter only: don't buy stocks that already spiked >3% today
                                gap_pct = quote.get("change_pct", 0)
                                if gap_pct > 3.0:
                                    logger.warning(f"[GeoScan] {sym} skipped — already up {gap_pct:.1f}% today")
                                    continue

                                # Apply VIX + scenario health scaling to position size
                                base_risk = float(get_setting(db, "risk_per_trade_pct", user.id, "2.0"))
                                scaled_risk = ps.vix_position_scale(geo_vix, base_risk)
                                scaled_risk = ps.scenario_position_scale(scaled_risk, scenario_mult)
                                # Temporarily write scaled risk to DB so auto_trade picks it up
                                set_setting(db, "risk_per_trade_pct", str(scaled_risk), user.id)

                                auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)

                                # Restore original risk %
                                set_setting(db, "risk_per_trade_pct", str(base_risk), user.id)

                                if auto_result.get("success"):
                                    _geo_traded_today[sym] = today_str
                                    logger.info(
                                        f"[GeoScan] Trade: {sym} → BUY | risk={scaled_risk:.2f}% "
                                        f"(VIX={geo_vix:.1f}, scenario={scenario_health['status']})"
                                    )
                                    await broadcast({
                                        "type": "auto_trade",
                                        "user": user.username,
                                        "symbol": sym,
                                        "result": auto_result,
                                        "trigger": "geopolitical_macro",
                                        "macro": macro["name"],
                                        "scenario_health": scenario_health["status"],
                                        "vix": geo_vix,
                                    })

                # ── Tech / Semiconductor News Scan ───────────────────────────
                tech_impacts = ni.detect_tech_market_impacts(hours_back=2)
                if tech_impacts:
                    seen_impact_titles = getattr(background_news_scan, "_seen_tech_titles", set())
                    new_impacts = [i for i in tech_impacts if i["title"] not in seen_impact_titles]
                    affected_syms = set()
                    for imp in new_impacts:
                        for s in imp["affected_stocks"]:
                            if s in watchlist:
                                affected_syms.add(s)
                    
                    if affected_syms:
                        for sym in affected_syms:
                            quote = price_cache.get(sym) or md.get_stock_quote(sym)
                            if not quote: continue
                            
                            history = md.get_stock_history(sym, period="1mo")
                            indicators = md.get_technical_indicators(sym)
                            news_items = md.get_stock_news(sym)
                            
                            engine = TradingEngine(db, user.id)
                            portfolio_ctx = build_rich_portfolio_context(db, user.id, engine)
                            tech_context = ni.build_tech_impact_context(sym, new_impacts)
                            
                            sector = ni.get_symbol_sector(sym)
                            signal = ai.analyze_stock(
                                ai_provider, api_key, sym, quote,
                                indicators, history, news_items,
                                portfolio_ctx, tech_context,
                                rl_lessons=rl_lessons,
                                sector=sector,
                                global_context=gc.build_global_context()
                            )
                            signal["sector"] = sector

                            rl.record_signal_state(
                                signal, quote, indicators or {},
                                tech_context, portfolio_ctx,
                                catalysts=[],
                                active_macros=[],
                                sector=sector
                            )
                           
                            db_signal = AISignal(
                                user_id=user.id,
                                symbol=sym,
                                signal=signal.get("signal", "HOLD"),
                                confidence=signal.get("confidence", 0),
                                target_price=signal.get("target_price"),
                                stop_loss=signal.get("stop_loss"),
                                reasoning=f"[TECH NEWS] {signal.get('reasoning', '')}",
                                model_used=signal.get("model", "unknown")
                            )
                            db.add(db_signal)
                            db.commit()
                            
                            if signal.get("signal") in ("BUY", "SELL", "COVER"):
                                auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
                                if auto_result.get("success"):
                                    await broadcast({
                                        "type": "auto_trade",
                                        "user": user.username,
                                        "symbol": sym,
                                        "result": auto_result,
                                        "trigger": "tech_news",
                                        "headline": new_impacts[0]["title"]
                                    })
                    
                    # Mark seen
                    for imp in new_impacts:
                        seen_impact_titles.add(imp["title"])
                    background_news_scan._seen_tech_titles = seen_impact_titles

                # Also backfill RL outcomes once per day (run at ~midnight UTC)
                if datetime.utcnow().hour == 0 and datetime.utcnow().minute < 10:
                    rl.update_trade_outcomes()
                    _run_daily_maintenance(db)

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


async def background_pending_trade_executor():
    """
    Execute queued pending trades (next-day catalyst orders) during market open window.
    """
    await asyncio.sleep(150)
    while True:
        db = None
        try:
            now = datetime.utcnow()
            if not _within_market_open_window(now):
                await asyncio.sleep(60)
                continue

            db = next(get_db())
            pendings = db.query(PendingTrade).filter(
                PendingTrade.status == "PENDING",
                PendingTrade.execute_on <= now,
            ).order_by(PendingTrade.execute_on.asc()).limit(50).all()

            for pending in pendings:
                try:
                    engine = TradingEngine(db, pending.user_id)
                    quote = price_cache.get(pending.symbol) or md.get_stock_quote(pending.symbol)
                    if not quote:
                        pending.last_error = "No market quote available"
                        db.commit()
                        continue

                    current_price = quote["current"] if isinstance(quote, dict) else quote
                    risk_pct = float(get_setting(db, "risk_per_trade_pct", pending.user_id, "2.0"))
                    cash = max(engine.get_cash_balance(), 0.0)
                    order_value = max(50.0, cash * (risk_pct / 100.0))
                    quantity = round(order_value / max(current_price, 0.01), 4)

                    if quantity < 0.0001:
                        pending.status = "FAILED"
                        pending.last_error = "Calculated quantity too small"
                        db.commit()
                        continue

                    if pending.side == "BUY":
                        result = engine.execute_buy(
                            pending.symbol, quantity, current_price,
                            ai_triggered=True, confidence=1.0, reasoning=pending.reason
                        )
                    else:
                        result = engine.execute_sell(
                            pending.symbol, quantity, current_price,
                            ai_triggered=True, confidence=1.0, reasoning=pending.reason
                        )

                    if result.get("success"):
                        pending.status = "EXECUTED"
                        pending.executed_at = datetime.utcnow()
                        pending.last_error = None
                    else:
                        pending.status = "FAILED"
                        pending.last_error = result.get("error") or result.get("reason") or "Execution failed"
                    db.commit()
                except Exception as inner_e:
                    logger.error(f"[PendingTrade] Error executing pending id={pending.id}: {inner_e}")
                    pending.status = "FAILED"
                    pending.last_error = str(inner_e)
                    db.commit()
        except Exception as e:
            logger.error(f"[PendingTrade] Loop error: {e}")
        finally:
            if db:
                db.close()

        await asyncio.sleep(60)


def _run_daily_maintenance(db):
    """
    Daily data housekeeping — runs once at midnight UTC.

    1. AI Signals older than 90 days:
       → Compress into weekly summaries (SignalArchive table) → delete raw rows
    2. Kronos prediction JSON files older than 90 days:
       → gzip-compress, delete originals
    3. Log file /tmp/alphatrader.log:
       → If > 200 MB, extract error/trade summary, rotate to .old, start fresh
    """
    import gzip, shutil, os
    from database import SignalArchive

    cutoff_90d = datetime.utcnow() - timedelta(days=90)
    logger.info("[Maintenance] Starting daily data housekeeping...")

    # ── 1. Compress AI signals older than 90 days ────────────────────────────
    try:
        old_signals = (
            db.query(AISignal)
            .filter(AISignal.timestamp < cutoff_90d)
            .order_by(AISignal.timestamp)
            .all()
        )
        if old_signals:
            # Group by (user_id, symbol, ISO week)
            from collections import defaultdict
            week_groups = defaultdict(list)
            for s in old_signals:
                # Monday of that week
                week_start = s.timestamp - timedelta(days=s.timestamp.weekday())
                week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
                week_groups[(s.user_id, s.symbol, week_start)].append(s)

            archived_count = 0
            for (uid, sym, wstart), signals in week_groups.items():
                wend = wstart + timedelta(days=6, hours=23, minutes=59)
                counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
                confs = []
                best = None
                for s in signals:
                    counts[s.signal] = counts.get(s.signal, 0) + 1
                    if s.confidence:
                        confs.append(s.confidence)
                    if best is None or (s.confidence or 0) > (best.confidence or 0):
                        best = s

                dominant = max(counts, key=counts.get)
                avg_conf = sum(confs) / len(confs) if confs else 0.0
                max_conf = max(confs) if confs else 0.0
                top_reasoning = (best.reasoning or "")[:300] if best else ""

                # Upsert archive row
                existing = db.query(SignalArchive).filter(
                    SignalArchive.user_id == uid,
                    SignalArchive.symbol == sym,
                    SignalArchive.week_start == wstart,
                ).first()
                if existing:
                    existing.total_signals += len(signals)
                    existing.buy_count += counts["BUY"]
                    existing.sell_count += counts["SELL"]
                    existing.hold_count += counts["HOLD"]
                    existing.avg_confidence = avg_conf
                    existing.max_confidence = max_conf
                    existing.dominant_signal = dominant
                    existing.top_reasoning = top_reasoning
                else:
                    db.add(SignalArchive(
                        user_id=uid, symbol=sym,
                        week_start=wstart, week_end=wend,
                        total_signals=len(signals),
                        buy_count=counts["BUY"],
                        sell_count=counts["SELL"],
                        hold_count=counts["HOLD"],
                        avg_confidence=avg_conf,
                        max_confidence=max_conf,
                        dominant_signal=dominant,
                        top_reasoning=top_reasoning,
                    ))
                archived_count += len(signals)

            # Delete raw signals
            db.query(AISignal).filter(AISignal.timestamp < cutoff_90d).delete()
            db.commit()
            logger.info(
                f"[Maintenance] Archived {archived_count} AI signals into "
                f"{len(week_groups)} weekly summaries; raw rows deleted."
            )
        else:
            logger.info("[Maintenance] No AI signals older than 90 days to archive.")
    except Exception as e:
        logger.error(f"[Maintenance] Signal archive error: {e}")
        db.rollback()

    # ── 2. Gzip Kronos prediction files older than 90 days ───────────────────
    try:
        pred_dir = "/data/qbao775/AlphaTrader/kronos_lib/webui/prediction_results"
        if os.path.isdir(pred_dir):
            compressed = 0
            for fname in os.listdir(pred_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(pred_dir, fname)
                age_days = (datetime.utcnow().timestamp() - os.path.getmtime(fpath)) / 86400
                if age_days > 90:
                    gz_path = fpath + ".gz"
                    with open(fpath, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    os.remove(fpath)
                    compressed += 1
            if compressed:
                logger.info(f"[Maintenance] Compressed {compressed} Kronos prediction files (>90d) to .gz")
    except Exception as e:
        logger.error(f"[Maintenance] Kronos file compression error: {e}")

    # ── 3. Log rotation if > 200 MB ─────────────────────────────────────────
    try:
        log_path = "/tmp/alphatrader.log"
        if os.path.exists(log_path):
            size_mb = os.path.getsize(log_path) / (1024 * 1024)
            if size_mb > 200:
                # Extract last 500 lines as summary before rotating
                with open(log_path, "rb") as f:
                    # Read last chunk
                    try:
                        f.seek(-min(500000, os.path.getsize(log_path)), 2)
                    except OSError:
                        f.seek(0)
                    tail_bytes = f.read()
                tail_text = tail_bytes.decode("utf-8", errors="replace")
                tail_lines = tail_text.splitlines()[-500:]

                # Save summary
                summary_path = f"/tmp/alphatrader_summary_{datetime.utcnow().strftime('%Y%m%d')}.log"
                with open(summary_path, "w") as sf:
                    sf.write(f"=== AlphaTrader Log Rotation Summary ({datetime.utcnow().isoformat()}) ===\n")
                    sf.write(f"Original size: {size_mb:.1f} MB | Last 500 lines preserved:\n\n")
                    sf.write("\n".join(tail_lines))

                # Compress old log
                old_gz = f"/tmp/alphatrader_{datetime.utcnow().strftime('%Y%m%d')}.log.gz"
                with open(log_path, "rb") as f_in, gzip.open(old_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

                # Truncate current log
                with open(log_path, "w") as f:
                    f.write(f"[{datetime.utcnow().isoformat()}] Log rotated. Previous {size_mb:.1f}MB archived to {old_gz}\n")

                logger.info(f"[Maintenance] Log rotated: {size_mb:.1f}MB → {old_gz}")
    except Exception as e:
        logger.error(f"[Maintenance] Log rotation error: {e}")

    # ── 4. Run RL Intelligence Attribution Analysis ──────────────────────────
    try:
        import intelligence_feedback as ifb
        ifb.run_attribution_analysis()
        logger.info("[Maintenance] RL Intelligence Attribution analysis complete.")
    except Exception as e:
        logger.error(f"[Maintenance] RL attribution error: {e}")

    logger.info("[Maintenance] Daily housekeeping complete.")


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

@app.get("/api/auth/alpaca/login")
async def alpaca_login():
    client_id = os.environ.get("ALPACA_OAUTH_CLIENT_ID")
    redirect_uri = os.environ.get("ALPACA_OAUTH_REDIRECT_URI", "http://localhost:8000/api/auth/alpaca/callback")
    if not client_id:
        raise HTTPException(status_code=500, detail="Alpaca OAuth not configured on server")
    url = f"https://app.alpaca.markets/oauth/authorize?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}&scope=account:write%20trading"
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)

@app.get("/api/auth/alpaca/callback")
async def alpaca_callback(code: str, db: Session = Depends(get_db)):
    client_id = os.environ.get("ALPACA_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("ALPACA_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get("ALPACA_OAUTH_REDIRECT_URI", "http://localhost:8000/api/auth/alpaca/callback")
    
    import httpx
    from fastapi.responses import RedirectResponse
    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_res = await client.post("https://api.alpaca.markets/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="OAuth token exchange failed")
            
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        
        # Fetch user account info
        account_res = await client.get("https://api.alpaca.markets/v2/account", headers={
            "Authorization": f"Bearer {access_token}"
        })
        if account_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch Alpaca account")
            
        account_data = account_res.json()
        account_number = account_data.get("account_number")
        
        # Find or create user
        username = f"alpaca_{account_number}"
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                username=username,
                hashed_password=get_password_hash(os.urandom(16).hex()),
                balance=0.0
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            
            defaults = {
                "auto_trade_enabled": "false",
                "auto_trade_min_confidence": "0.75",
                "risk_per_trade_pct": "2.0",
                "ai_provider": "ollama",
                "watchlist": json.dumps(md.DEFAULT_WATCHLIST),
            }
            for k, v in defaults.items():
                set_setting(db, k, v, user.id)
                
        # Save OAuth token for this user
        set_setting(db, "alpaca_oauth_token", access_token, user.id)
        
        # We also need to map this to an internal JWT so the frontend can stay mostly the same
        internal_jwt = create_access_token(data={"sub": user.username})
        
        # Redirect back to frontend
        return RedirectResponse(f"/?token={internal_jwt}")
@app.get("/api/auth/auto-login")
async def dummy_auto_login():
    raise HTTPException(status_code=401, detail="Legacy auto-login disabled")


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
    """Get all global market indices with market open/close status."""
    global market_cache, last_market_fetch
    now = datetime.utcnow()
    if not market_cache or last_market_fetch is None or (now - last_market_fetch).seconds > 300:
        try:
            market_cache = md.get_all_indices()
            last_market_fetch = now
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"data": market_cache, "timestamp": now.isoformat()}


@app.get("/api/global-context")
async def get_global_context(current_user: User = Depends(get_current_user)):
    """
    Return the current global market context snapshot (VIX, risk env, sector rotation,
    cross-market signals, confidence modifiers, northbound capital, etc.).
    Cached for 5 minutes; forces a refresh if cache is stale.
    """
    loop = asyncio.get_event_loop()
    try:
        ctx = await loop.run_in_executor(None, gc.build_global_context)
        # Strip the large ai_narrative from the API response (it's for internal AI use)
        resp = {k: v for k, v in ctx.items() if k != "ai_narrative"}
        resp["summary"] = gc.get_global_context_summary(ctx)
        return resp
    except Exception as e:
        logger.error(f"[GlobalContext] API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/market-status")
async def get_market_status():
    """
    Return real-time open/closed status for all global exchanges.
    Includes local time, currency, and session hours.
    """
    from market_calendar import get_all_market_statuses, get_market_open_count
    statuses = get_all_market_statuses()
    counts = get_market_open_count()
    return {"markets": statuses, "summary": counts, "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/markets/popular-stocks")
async def get_popular_stocks(region: str = None):
    """
    Return popular international stock symbols by region.
    region: US_TECH, US_FINANCE, HK, CN_ASHARE, JP, EU, AU, KR, IN, BR, SG
    """
    stocks = md.get_global_popular_stocks(region)
    return {"region": region or "all", "symbols": stocks}


@app.get("/api/markets/news")
async def get_global_news():
    """Fetch latest news bucketed by market region (CN, HK, JP, EU, US, EM, GLOBAL)."""
    loop = asyncio.get_event_loop()
    news_map = await loop.run_in_executor(None, lambda: ni.fetch_global_market_news(hours_back=8))
    return {"data": news_map, "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/broker-status")
async def get_broker_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Check connection status for all configured brokers."""
    from futu_broker import create_futu_broker_from_settings
    from ibkr_broker import create_ibkr_broker_from_settings

    settings = {}
    from database import Settings as SettingsModel
    rows = db.query(SettingsModel).filter(SettingsModel.user_id == current_user.id).all()
    for r in rows:
        settings[r.key] = r.value

    # Alpaca
    alpaca_key = settings.get("alpaca_api_key", "")
    oauth = settings.get("alpaca_oauth_token", "")
    alpaca_ok = bool(alpaca_key or oauth)
    alpaca_live = settings.get("alpaca_paper_mode", "true") != "true"

    # Futu
    futu_enabled = settings.get("futu_enabled", "false") == "true"
    futu_connected = False
    if futu_enabled:
        try:
            fb = create_futu_broker_from_settings(settings)
            futu_connected = fb.is_connected()
        except Exception:
            pass

    # IBKR
    ibkr_enabled = settings.get("ibkr_enabled", "false") == "true"
    ibkr_connected = False
    if ibkr_enabled:
        try:
            ib = create_ibkr_broker_from_settings(settings)
            ibkr_connected = ib.is_connected()
        except Exception:
            pass

    return {
        "alpaca": {
            "configured": alpaca_ok,
            "live_mode": alpaca_live,
            "markets": ["US"],
            "status": "active" if alpaca_ok else "not_configured",
        },
        "futu": {
            "enabled": futu_enabled,
            "connected": futu_connected,
            "markets": ["CN", "HK", "US"],
            "trade_env": settings.get("futu_trade_env", "SIMULATE"),
            "status": "connected" if futu_connected else ("enabled_offline" if futu_enabled else "disabled"),
        },
        "ibkr": {
            "enabled": ibkr_enabled,
            "connected": ibkr_connected,
            "markets": ["US", "HK", "JP", "GB", "DE", "FR", "AU", "KR", "SG", "IN", "BR", "CA"],
            "status": "connected" if ibkr_connected else ("enabled_offline" if ibkr_enabled else "disabled"),
        },
        "paper": {
            "active": not (alpaca_ok or futu_connected or ibkr_connected),
            "markets": ["ALL"],
            "status": "active",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


import asyncio

@app.get("/api/stock/{symbol}")
async def get_stock(symbol: str, period: str = "3mo"):
    """Get full data for a single stock (all markets: US, CN, HK, JP, EU, ...)."""
    # Preserve original case for A-shares (600519.SH) but uppercase US symbols
    from market_calendar import detect_market
    if "." not in symbol:
        symbol = symbol.upper()
    else:
        parts = symbol.rsplit(".", 1)
        symbol = parts[0] + "." + parts[1].upper()
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
    rl_lessons = get_rl_lessons()
    symbol = request.symbol.upper()
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    ai_provider = get_setting(db, "ai_provider", current_user.id, "ollama")

    quote = md.get_stock_quote(symbol)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    history = md.get_stock_history(symbol, period="6mo")
    indicators = md.get_technical_indicators(symbol)
    news = md.get_stock_news(symbol)
    sector = ni.get_symbol_sector(symbol)

    # Portfolio context
    engine = TradingEngine(db, current_user.id)
    summary = engine.get_portfolio_summary()
    portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"

    signal = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context, rl_lessons=rl_lessons, sector=sector, global_context=gc.build_global_context())
    signal["sector"] = sector

    # Record to RL training dataset
    rl.record_signal_state(
        signal, quote, indicators or {}, 
        "Manual Analysis", portfolio_context,
        catalysts=[],
        active_macros=[],
        sector=sector
    )
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
        auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
        if auto_result.get("success"):
            await broadcast({"type": "auto_trade", "user": current_user.username, "signal": signal, "trade": auto_result})

    return {"signal": signal, "quote": quote, "auto_trade": auto_result}


@app.post("/api/analyze-portfolio")
async def analyze_portfolio(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Run DeepSeek-R1 portfolio analysis."""
    api_key = get_setting(db, "deepseek_api_key", current_user.id, "")
    ai_provider = get_setting(db, "ai_provider", current_user.id, "ollama")
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
    ai_provider = get_setting(db, "ai_provider", current_user.id, "ollama")
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
            ai_provider = get_setting(db, "ai_provider", "ollama")
            engine = TradingEngine(db)
            summary = engine.get_portfolio_summary()
            portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"
            
            import deepseek_ai as ai
            import global_context as _gc
            signal_data = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context, rl_lessons=rl_lessons, global_context=_gc.build_global_context())
            
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
    """Get all settings (sensitive keys are masked)."""
    keys = [
        "auto_trade_enabled", "auto_trade_min_confidence",
        "risk_per_trade_pct", "refresh_interval_seconds", "ai_provider",
        "alpaca_paper_mode", "allow_short_selling", "stop_loss_pct",
        # Multi-market broker settings
        "futu_enabled", "futu_host", "futu_port", "futu_trade_env",
        "futu_cn_acc_id", "futu_hk_acc_id", "futu_us_acc_id",
        "ibkr_enabled", "ibkr_host", "ibkr_port", "ibkr_client_id", "ibkr_account",
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


class FutuConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11111
    trade_env: str = "SIMULATE"   # "REAL" or "SIMULATE"
    cn_acc_id: str = ""
    hk_acc_id: str = ""
    us_acc_id: str = ""
    enabled: bool = True


class IBKRConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 10
    account: str = ""
    enabled: bool = True


@app.post("/api/broker/futu/configure")
async def configure_futu(
    cfg: FutuConfig,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Configure Futu OpenD broker for China A-shares / HK stocks.
    Requires Futu OpenD daemon running at the specified host:port.
    Install SDK: pip install futu-api
    """
    set_setting(db, "futu_enabled",     str(cfg.enabled).lower(), current_user.id)
    set_setting(db, "futu_host",        cfg.host,                  current_user.id)
    set_setting(db, "futu_port",        str(cfg.port),             current_user.id)
    set_setting(db, "futu_trade_env",   cfg.trade_env,             current_user.id)
    set_setting(db, "futu_cn_acc_id",   cfg.cn_acc_id,             current_user.id)
    set_setting(db, "futu_hk_acc_id",   cfg.hk_acc_id,             current_user.id)
    set_setting(db, "futu_us_acc_id",   cfg.us_acc_id,             current_user.id)

    # Test connectivity
    connected = False
    if cfg.enabled:
        try:
            from futu_broker import FutuBroker
            fb = FutuBroker(host=cfg.host, port=cfg.port, trade_env=cfg.trade_env)
            connected = fb.is_connected()
        except Exception as e:
            logger.warning(f"[Futu Config] Connection test failed: {e}")

    return {
        "configured": True,
        "connected": connected,
        "trade_env": cfg.trade_env,
        "markets": ["CN (A股)", "HK (港股)", "US (美股)"],
        "note": "SIMULATE mode safe for testing; set trade_env=REAL for live trading",
    }


@app.post("/api/broker/ibkr/configure")
async def configure_ibkr(
    cfg: IBKRConfig,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Configure Interactive Brokers for global markets.
    Requires IBKR TWS or Gateway running at the specified host:port.
    Install SDK: pip install ib_insync
    TWS paper port: 7497 | TWS live port: 7496
    Gateway paper port: 4002 | Gateway live port: 4001
    """
    set_setting(db, "ibkr_enabled",    str(cfg.enabled).lower(),  current_user.id)
    set_setting(db, "ibkr_host",       cfg.host,                  current_user.id)
    set_setting(db, "ibkr_port",       str(cfg.port),             current_user.id)
    set_setting(db, "ibkr_client_id",  str(cfg.client_id),        current_user.id)
    set_setting(db, "ibkr_account",    cfg.account,               current_user.id)

    connected = False
    if cfg.enabled:
        try:
            from ibkr_broker import IBKRBroker
            ib = IBKRBroker(host=cfg.host, port=cfg.port, client_id=cfg.client_id, account=cfg.account)
            connected = ib.is_connected()
        except Exception as e:
            logger.warning(f"[IBKR Config] Connection test failed: {e}")

    return {
        "configured": True,
        "connected": connected,
        "markets": ["US", "HK", "JP", "GB", "DE", "FR", "AU", "KR", "SG", "IN", "BR", "CA", "more..."],
        "note": "Paper port 7497 for TWS; start TWS/Gateway before connecting",
    }


class EmailConfig(BaseModel):
    sender: str
    app_password: str
    recipient: str
    enabled: bool = True


@app.post("/api/email/configure")
async def configure_email(cfg: EmailConfig, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Configure email reporter credentials."""
    set_setting(db, "email_sender", cfg.sender, current_user.id)
    set_setting(db, "email_app_password", cfg.app_password, current_user.id)
    set_setting(db, "email_recipient", cfg.recipient, current_user.id)
    set_setting(db, "email_enabled", str(cfg.enabled).lower(), current_user.id)
    return {"configured": True, "recipient": cfg.recipient}


@app.post("/api/email/test")
async def test_email(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Send a test email to verify configuration."""
    settings = {s.key: s.value for s in db.query(Settings).filter_by(user_id=current_user.id).all()}
    sender = settings.get("email_sender", "")
    app_pw = settings.get("email_app_password", "")
    recipient = settings.get("email_recipient", "")
    if not (sender and app_pw and recipient):
        raise HTTPException(status_code=400, detail="Email not configured. Call /api/email/configure first.")
    _test_gc = {}
    try:
        _test_gc = gc.build_global_context()
    except Exception:
        pass
    html = er.generate_report_html(
        datetime.utcnow().strftime("%Y-%m-%d (Test)"),
        {"equity": 376.72, "cash": 326.63, "unrealized_pl": 2.34},
        [{"symbol": "GLD", "qty": 0.038, "avg_entry_price": 242.0, "current_price": 251.5, "unrealized_pl": 0.36, "unrealized_plpc": 3.92}],
        [{"symbol": "LMT", "signal": "BUY", "confidence": 0.90, "reasoning": "Significant undervaluation, defence demand surge.", "timestamp": datetime.utcnow().isoformat()}],
        [{"name": "中东战争 2026", "severity": "CRITICAL", "beneficiaries": ["GLD", "LMT", "RTX"]}],
        [{"symbol": "LMT", "action": "BUY", "confidence": 0.90, "reason": "AI BUY 90% — undervaluation -57%"}],
        global_context=_test_gc,
        scenario_healths=[{"name": "中东战争 2026", "status": "failing", "avg_pct": -12.5, "days_active": 22, "per_stock_summary": "GLD -14.5% | LMT -4.6% | RTX -2.2%"}],
        global_scan_signals=[{"symbol": "EWJ", "region": "JP", "signal": "BUY", "confidence": 0.78, "reasoning": "Japan equities oversold, BOJ pivot tailwind, USD/JPY correction expected.", "timestamp": datetime.utcnow().isoformat()}],
    )
    sent = er.send_email(sender, app_pw, recipient, "AlphaTrader — Test Email", html)
    if sent:
        return {"sent": True, "recipient": recipient}
    raise HTTPException(status_code=500, detail="Failed to send email. Check App Password and try again.")


@app.post("/api/layoff-framework/evaluate")
async def evaluate_layoff_framework(
    payload: LayoffFrameworkRequest,
    _current_user: User = Depends(get_current_user),
):
    """
    Quantify market reactions around layoff announcements.
    Tracks event-window returns, reaction duration, and a composite strength score.
    """
    if not payload.events:
        raise HTTPException(status_code=400, detail="events must not be empty")
    if payload.lookahead_days < 1 or payload.lookahead_days > 60:
        raise HTTPException(status_code=400, detail="lookahead_days must be between 1 and 60")

    events = [e.dict() for e in payload.events]
    return lef.analyze_layoff_events(
        events=events,
        benchmark_symbol=payload.benchmark_symbol.upper(),
        lookahead_days=payload.lookahead_days,
    )


@app.post("/api/layoff-framework/discover")
async def discover_layoff_candidates(
    payload: LayoffDiscoveryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Semi-auto discovery of layoff/restructuring headlines.
    Returns candidate events for manual confirmation/import.
    """
    if payload.hours_back < 1 or payload.hours_back > 24 * 90:
        raise HTTPException(status_code=400, detail="hours_back must be between 1 and 2160")
    if payload.max_items < 1 or payload.max_items > 200:
        raise HTTPException(status_code=400, detail="max_items must be between 1 and 200")

    symbols = [s.upper() for s in (payload.symbols or []) if s]
    if payload.use_watchlist:
        watchlist_json = get_setting(db, "watchlist", current_user.id, json.dumps(md.DEFAULT_WATCHLIST))
        try:
            watchlist = json.loads(watchlist_json)
        except Exception:
            watchlist = md.DEFAULT_WATCHLIST
        symbols.extend([s.upper() for s in watchlist if s])

    symbols = sorted(set(symbols))
    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols to scan")

    return lef.discover_layoff_candidates(
        symbols=symbols,
        hours_back=payload.hours_back,
        max_items=payload.max_items,
    )


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


async def background_email_reporter():
    """
    Task 9 — Send daily portfolio + AI signal report via email.
    Fires once per day shortly after US market close (21:10 UTC = 16:10 EST).
    """
    await asyncio.sleep(60)
    last_sent_date = None
    while True:
        try:
            now = datetime.utcnow()
            # Send at 21:10 UTC (after US close), Mon-Fri
            if (now.weekday() < 5
                    and now.hour == 21 and 10 <= now.minute < 20
                    and last_sent_date != now.date()):
                db = next(get_db())
                try:
                    settings = {s.key: s.value for s in db.query(Settings).filter_by(user_id=1).all()}
                    email_enabled = settings.get("email_enabled", "false").lower() == "true"
                    sender = settings.get("email_sender", "")
                    app_pw = settings.get("email_app_password", "")
                    recipient = settings.get("email_recipient", "")

                    if not (email_enabled and sender and app_pw and recipient):
                        await asyncio.sleep(600)
                        continue

                    # Gather Alpaca account data
                    alpaca_account = {"equity": 0, "cash": 0, "unrealized_pl": 0}
                    try:
                        from trading_engine import TradingEngine
                        engine = TradingEngine(db, 1)
                        if engine.alpaca:
                            acct = engine.alpaca.get_account()
                            alpaca_account = {
                                "equity": float(acct.equity),
                                "cash": float(acct.cash),
                                "unrealized_pl": float(acct.equity) - float(acct.last_equity),
                            }
                            raw_positions = engine.alpaca.list_positions()
                            positions = [
                                {
                                    "symbol": p.symbol,
                                    "qty": float(p.qty),
                                    "avg_entry_price": float(p.avg_entry_price),
                                    "current_price": float(p.current_price),
                                    "unrealized_pl": float(p.unrealized_pl),
                                    "unrealized_plpc": float(p.unrealized_plpc) * 100,
                                }
                                for p in raw_positions
                            ]
                    except Exception as e:
                        logger.warning(f"[EmailReport] Alpaca fetch error: {e}")
                        positions = []

                    # Gather today's AI signals
                    since = datetime.utcnow() - timedelta(hours=24)
                    signals = [
                        {
                            "symbol": s.symbol,
                            "signal": s.signal,
                            "confidence": s.confidence,
                            "reasoning": s.reasoning or "",
                            "timestamp": s.timestamp.isoformat() if s.timestamp else "",
                        }
                        for s in db.query(AISignal)
                            .filter(AISignal.user_id == 1, AISignal.timestamp >= since)
                            .order_by(AISignal.timestamp.desc())
                            .limit(15)
                            .all()
                    ]

                    # Gather active macro scenarios
                    macro_scenarios = []
                    try:
                        import news_intelligence as ni_local
                        scenarios = await asyncio.get_event_loop().run_in_executor(
                            None, ni_local.detect_active_macro_scenarios, None
                        )
                        for name, info in (scenarios or {}).items():
                            macro_scenarios.append({
                                "name": name,
                                "severity": info.get("severity", "LOW"),
                                "beneficiaries": info.get("beneficiaries", []),
                            })
                    except Exception:
                        pass

                    # Planned trades = highest-confidence BUY/SELL signals
                    # Planned trades: highest-confidence BUY/SELL signals with target/stop
                    planned_trades = []
                    for s in signals:
                        if s["signal"] in ("BUY", "SELL") and s["confidence"] >= 0.75:
                            # Fetch target_price + stop_loss from DB signal record
                            db_sig = db.query(AISignal).filter(
                                AISignal.user_id == 1,
                                AISignal.symbol == s["symbol"],
                                AISignal.signal == s["signal"],
                            ).order_by(AISignal.timestamp.desc()).first()
                            planned_trades.append({
                                "symbol": s["symbol"],
                                "action": s["signal"],
                                "confidence": s["confidence"],
                                "reason": s["reasoning"][:120],
                                "target_price": float(db_sig.target_price) if db_sig and db_sig.target_price else None,
                                "stop_loss": float(db_sig.stop_loss) if db_sig and db_sig.stop_loss else None,
                            })
                            if len(planned_trades) >= 6:
                                break

                    # Yesterday's executed trades (last 24h from DB)
                    yesterday_trades = [
                        {
                            "symbol": t.symbol,
                            "side": t.side,
                            "quantity": t.quantity,
                            "price": t.price,
                            "total_value": t.total_value,
                            "ai_confidence": t.ai_confidence,
                            "reasoning": t.reasoning or "",
                            "timestamp": t.timestamp.isoformat() if t.timestamp else "",
                        }
                        for t in db.query(Trade)
                            .filter(Trade.user_id == 1, Trade.timestamp >= since, Trade.status == "filled")
                            .order_by(Trade.timestamp.desc())
                            .all()
                    ]

                    # Market regime for tomorrow's plan header
                    try:
                        spy_ind = await asyncio.get_event_loop().run_in_executor(None, md.get_technical_indicators, "SPY")
                        spy_q   = price_cache.get("SPY") or {}
                        spy_px  = spy_q.get("current", 0)
                        spy_ma20 = (spy_ind or {}).get("ma20", 0)
                        market_regime = "BEAR" if (spy_px and spy_ma20 and spy_px < spy_ma20) else "BULL"
                    except Exception:
                        market_regime = "NORMAL"

                    # ── Global context for email ──────────────────────────────
                    email_global_ctx = {}
                    try:
                        email_global_ctx = gc.build_global_context()
                    except Exception as _gce:
                        logger.warning(f"[EmailReport] Global context error: {_gce}")

                    # ── Scenario health for each active macro ─────────────────
                    email_scenario_healths = []
                    try:
                        for mac in macro_scenarios:
                            health = st.get_scenario_health(
                                mac.get("name", ""),
                                mac.get("beneficiaries", []),
                                db, 1, price_cache,
                            )
                            per_stock = health.get("context_str", "").split(
                                "Per-stock since first trade: "
                            )
                            per_stock_summary = per_stock[1][:120] if len(per_stock) > 1 else ""
                            email_scenario_healths.append({
                                "name":             mac.get("name", ""),
                                "status":           health["status"],
                                "avg_pct":          health["avg_pct"],
                                "days_active":      health["days_active"],
                                "per_stock_summary": per_stock_summary,
                            })
                    except Exception as _she:
                        logger.warning(f"[EmailReport] Scenario health error: {_she}")

                    # ── Global scan signals (last 24h, BUY only) ──────────────
                    email_global_signals = []
                    try:
                        global_sigs_raw = (
                            db.query(AISignal)
                            .filter(
                                AISignal.user_id == 1,
                                AISignal.timestamp >= since,
                                AISignal.reasoning.like("%[GLOBAL SCAN%"),
                            )
                            .order_by(AISignal.confidence.desc())
                            .limit(12)
                            .all()
                        )
                        for gs in global_sigs_raw:
                            # Extract region from reasoning tag e.g. "[GLOBAL SCAN/HK]"
                            region = "US"
                            import re as _re
                            m = _re.search(r"\[GLOBAL SCAN/([^\]]+)\]", gs.reasoning or "")
                            if m:
                                region = m.group(1)
                            email_global_signals.append({
                                "symbol":    gs.symbol,
                                "signal":    gs.signal,
                                "confidence": gs.confidence,
                                "reasoning": gs.reasoning or "",
                                "timestamp": gs.timestamp.isoformat() if gs.timestamp else "",
                                "region":    region,
                            })
                    except Exception as _gse:
                        logger.warning(f"[EmailReport] Global scan signals error: {_gse}")

                    date_str = now.strftime("%Y-%m-%d %A")
                    html = er.generate_report_html(
                        date_str, alpaca_account, positions,
                        signals, macro_scenarios, planned_trades,
                        yesterday_trades=yesterday_trades,
                        market_regime=market_regime,
                        global_context=email_global_ctx,
                        scenario_healths=email_scenario_healths,
                        global_scan_signals=email_global_signals,
                    )
                    subject = f"AlphaTrader Daily Report — {now.strftime('%Y-%m-%d')}"
                    sent = er.send_email(sender, app_pw, recipient, subject, html)
                    if sent:
                        last_sent_date = now.date()
                        logger.info(f"[EmailReport] Daily report sent for {now.date()}")
                finally:
                    db.close()
        except Exception as e:
            logger.error(f"[EmailReport] Error: {e}")
        await asyncio.sleep(60)


async def background_email_reply_checker():
    """
    Task 10 — Real-time Gmail reply handler via IMAP IDLE.
    Server pushes a notification the moment a new email arrives;
    no polling delay. Reconnects automatically after each 14-min
    IDLE window (Gmail drops connections at 15 min) or on error.
    """
    await asyncio.sleep(120)
    mail_conn = None

    while True:
        try:
            # Load settings
            db = next(get_db())
            try:
                settings = {s.key: s.value for s in db.query(Settings).filter_by(user_id=1).all()}
            finally:
                db.close()

            email_enabled = settings.get("email_enabled", "false").lower() == "true"
            sender = settings.get("email_sender", "")
            app_pw = settings.get("email_app_password", "")

            if not (email_enabled and sender and app_pw):
                await asyncio.sleep(60)
                continue

            # (Re)connect if needed
            if mail_conn is None:
                mail_conn = await asyncio.get_event_loop().run_in_executor(
                    None, er.connect_imap, sender, app_pw
                )
                if mail_conn is None:
                    await asyncio.sleep(30)
                    continue
                logger.info("[EmailReply] IMAP IDLE connected — waiting for replies in real-time")

            # Block in IDLE until new mail or 14-min timeout
            new_mail = await asyncio.get_event_loop().run_in_executor(
                None, er.idle_wait, mail_conn, 840
            )

            if new_mail:
                logger.info("[EmailReply] New email detected via IDLE — checking for replies")
            else:
                logger.debug("[EmailReply] IDLE window expired — checking for missed replies")

            # Close IDLE connection first — its state is undefined after DONE
            try:
                mail_conn.logout()
            except Exception:
                pass
            mail_conn = None

            # Open a FRESH connection for SEARCH+FETCH (avoids IDLE state confusion)
            fresh_conn = await asyncio.get_event_loop().run_in_executor(
                None, er.connect_imap, sender, app_pw
            )
            if fresh_conn:
                try:
                    replies = await asyncio.get_event_loop().run_in_executor(
                        None, er._fetch_new_replies, fresh_conn
                    )
                finally:
                    try:
                        fresh_conn.logout()
                    except Exception:
                        pass
                if replies:
                    db = next(get_db())
                    try:
                        for reply in replies:
                            logger.info(f"[EmailReply] Processing: {reply['subject'][:60]}")
                            result = await er.process_reply_with_ai(reply["body"], db, settings)
                            logger.info(f"[EmailReply] Changes applied: {result}")
                    finally:
                        db.close()

        except Exception as e:
            logger.error(f"[EmailReply] Error: {e}")
            if mail_conn:
                try:
                    mail_conn.logout()
                except Exception:
                    pass
                mail_conn = None
            await asyncio.sleep(15)  # brief pause before reconnecting


async def background_global_market_scan():
    """
    Task 12 — Global Market Scanner.
    Runs every 20 minutes. Identifies which global markets are currently open,
    scores each region using global context (risk score, currency flows, index momentum),
    then runs AI analysis on the top candidate stocks from the best-performing regions.

    Tradeable globally:
    - Via Alpaca (always): US stocks + US-listed Global ETFs (EWJ, FXI, EWT, VGK, etc.)
    - Via Futu (if configured): HK + CN A-shares
    - Via IBKR (if configured): JP, EU, AU, KR, SG, IN, BR direct listings
    """
    await asyncio.sleep(360)  # 6-min startup delay — let price cache warm up first
    while True:
        try:
            loop = asyncio.get_event_loop()
            db = next(get_db())
            users = db.query(User).all()

            for user in users:
                auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                if not auto_trade_enabled:
                    continue

                api_key  = get_setting(db, "deepseek_api_key", user.id, "")
                ai_provider = get_setting(db, "ai_provider", user.id, "ollama")
                rl_lessons = get_rl_lessons()

                # ── 1. Build global context ──────────────────────────────────────
                global_ctx = await loop.run_in_executor(None, gc.build_global_context)
                risk_env   = global_ctx.get("risk_environment", "NEUTRAL")
                risk_score = global_ctx.get("risk_score", 0.0)
                vix_val    = global_ctx.get("vix", {}).get("value", 18)
                sector_rot = global_ctx.get("sector_rotation", {})

                # ── 2. Score each region based on live index momentum + flows ────
                def _chg(path):
                    """Extract % change from nested global_ctx dict."""
                    keys = path.split(".")
                    obj = global_ctx
                    for k in keys:
                        obj = (obj or {}).get(k, {})
                    return obj.get("change_pct", 0) or 0

                region_scores = {
                    "US":  0.4 + _chg("us_markets.sp500") * 0.05 + (0.1 if risk_score > 0 else -0.1),
                    "HK":  0.4 + _chg("asia_markets.hangseng") * 0.06,
                    "CN":  0.4 + _chg("china_markets.sse_composite") * 0.06
                          + (global_ctx.get("china_markets", {}).get("northbound_flow", {}).get("total_net_bn_cny", 0) or 0) * 0.005,
                    "JP":  0.4 + _chg("asia_markets.nikkei") * 0.05,
                    "EU":  0.4 + _chg("europe_markets.dax") * 0.05,
                    "AU":  0.4 + _chg("asia_markets.asx200") * 0.05,
                    "KR":  0.4 + _chg("asia_markets.kospi") * 0.05,
                    "IN":  0.4 + _chg("asia_markets.nifty50") * 0.05,
                    "GLOBAL_ETF": 0.5,  # always include ETFs as they cover global exposure
                }

                # ── 3. Check which markets are open right now ────────────────────
                from market_calendar import is_market_open
                region_to_buckets = {
                    "US":         ["US_TECH", "US_FINANCE", "US_ENERGY"],
                    "GLOBAL_ETF": ["GLOBAL_ETF"],
                    "HK":         ["HK"],
                    "CN":         ["CN_ASHARE"],
                    "JP":         ["JP"],
                    "EU":         ["EU"],
                    "AU":         ["AU"],
                    "KR":         ["KR"],
                    "IN":         ["IN"],
                }
                open_regions = []
                for region in region_to_buckets:
                    mkt = region if region != "GLOBAL_ETF" else "US"
                    try:
                        if is_market_open(mkt):
                            open_regions.append(region)
                    except Exception:
                        if region in ("US", "GLOBAL_ETF"):
                            open_regions.append(region)  # default-include US

                if not open_regions:
                    logger.info("[GlobalScan] No markets currently open — skipping cycle")
                    await asyncio.sleep(1200)
                    continue

                # ── 4. Rank open regions by score, pick top 3 ───────────────────
                ranked = sorted(
                    [(r, region_scores.get(r, 0.4)) for r in open_regions],
                    key=lambda x: x[1], reverse=True
                )
                top_regions = [r for r, s in ranked[:4]]  # top 4 regions
                logger.info(
                    f"[GlobalScan] Open markets: {open_regions} | "
                    f"Top regions: {top_regions} | risk={risk_env}({risk_score:+.2f}) VIX={vix_val:.1f}"
                )

                # ── 5. Build candidate list: PRIORITY-BASED (pyramid into winners) ─
                #
                # P1  Portfolio winners (PnL > +3%)       → add to these first
                # P2  Siblings in same sector/bucket       → ride the hot sector
                # P3  Fill up to 8 from top open region    → max 2/bucket, not 4
                #
                # Total cap = 8 (not 15). Concentrate on what's working.

                from database import Position as _Pos
                _sym_to_bucket: dict = {}
                for _bkt, _bkt_syms in md.GLOBAL_POPULAR_STOCKS.items():
                    for _s in _bkt_syms:
                        _sym_to_bucket[_s] = _bkt

                live_positions = (
                    db.query(_Pos)
                    .filter(_Pos.user_id == user.id, _Pos.quantity > 0.001)
                    .all()
                )
                portfolio_winners: list = []   # (sym, region, pnl_pct)
                winning_buckets: set = set()
                for _pos in live_positions:
                    _cur = (price_cache.get(_pos.symbol) or {}).get("current", 0)
                    if _cur and _pos.avg_cost:
                        _pnl = (_cur / _pos.avg_cost - 1) * 100
                        if _pnl >= 3.0:
                            portfolio_winners.append((_pos.symbol, "US", _pnl))
                            _bkt = _sym_to_bucket.get(_pos.symbol)
                            if _bkt:
                                winning_buckets.add(_bkt)
                portfolio_winners.sort(key=lambda x: x[2], reverse=True)

                candidates = []
                seen_syms: set = set()

                # P1 — current winners (pyramid into them)
                for sym, region, _pnl in portfolio_winners:
                    seen_syms.add(sym)
                    candidates.append((sym, region))

                # P2 — sibling stocks from same hot sector bucket
                for _bkt in winning_buckets:
                    for sym in md.GLOBAL_POPULAR_STOCKS.get(_bkt, []):
                        if sym not in seen_syms and len(candidates) < 6:
                            seen_syms.add(sym)
                            candidates.append((sym, "US"))

                # P3 — fill remaining slots from top-ranked open regions (max 2/bucket)
                if risk_env == "RISK_OFF":
                    top_regions = ["GLOBAL_ETF"] + [r for r in top_regions if r != "GLOBAL_ETF"]
                for region in top_regions:
                    for bucket in region_to_buckets.get(region, []):
                        stocks = md.GLOBAL_POPULAR_STOCKS.get(bucket, [])
                        added = 0
                        for sym in stocks:
                            if sym not in seen_syms and len(candidates) < 8 and added < 2:
                                seen_syms.add(sym)
                                candidates.append((sym, region))
                                added += 1
                    if len(candidates) >= 8:
                        break

                # ── 6. Analyze each candidate ────────────────────────────────────
                engine = TradingEngine(db, user.id)
                portfolio_ctx = build_rich_portfolio_context(db, user.id, engine)
                active_macros = ni.detect_active_macro_scenarios(hours_back=3)
                macro_ctx_str = ni.build_macro_scenario_context(active_macros)

                # Inject momentum focus directive into AI context
                if portfolio_winners:
                    winner_summary = ", ".join(
                        f"{s}(+{p:.1f}%)" for s, _, p in portfolio_winners
                    )
                    macro_ctx_str = (
                        "### MOMENTUM FOCUS DIRECTIVE\n"
                        f"Portfolio winners today: {winner_summary}\n"
                        "STRATEGY: Add to stocks already moving up. "
                        "Do NOT diversify into new unrelated positions — concentrate on strength.\n"
                        "Only BUY a new (unrelated) stock if it shows clearly superior signals "
                        "AND the existing winners are near resistance or overbought.\n\n"
                    ) + macro_ctx_str
                    logger.info(
                        f"[GlobalScan] Pyramid mode: winners={[s for s,_,_ in portfolio_winners]}, "
                        f"hot buckets={list(winning_buckets)}, candidates={[s for s,_ in candidates]}"
                    )

                for sym, region in candidates:
                    try:
                        await asyncio.sleep(1.5)  # rate-limit yfinance
                        quote = price_cache.get(sym) or await loop.run_in_executor(None, md.get_stock_quote, sym)
                        if not quote or not quote.get("current"):
                            continue

                        indicators = await loop.run_in_executor(None, md.get_technical_indicators, sym)
                        if not indicators:
                            continue

                        # Quick pre-filter: skip stocks in clear downtrend with no bounce
                        rsi = indicators.get("rsi", 50)
                        above_ma20 = indicators.get("above_ma20", True)
                        # In RISK_OFF allow oversold stocks (RSI<35) — potential bounce
                        if not above_ma20 and rsi > 45:
                            continue  # below MA20 and not even oversold — skip

                        history  = await loop.run_in_executor(None, md.get_stock_history, sym, "1mo")
                        news_items = await loop.run_in_executor(None, md.get_stock_news, sym)
                        sector   = ni.get_symbol_sector(sym)

                        # Compute ATR-based stop-loss for the AI context
                        atr = indicators.get("atr14", 0)
                        current_price = quote.get("current", 0)
                        adaptive_stop = ps.atr_stop_loss(current_price, atr) if current_price else None

                        # VIX-scaled risk for this cycle
                        base_risk = float(get_setting(db, "risk_per_trade_pct", user.id, "2.0"))
                        scaled_risk = ps.vix_position_scale(vix_val, base_risk)

                        global_note = (
                            f"\n### GLOBAL SCAN CONTEXT\n"
                            f"Region: {region} | Risk: {risk_env}({risk_score:+.2f}) | VIX: {vix_val:.1f}\n"
                            f"Position size auto-scaled to {scaled_risk:.2f}% of portfolio (VIX adjustment).\n"
                            f"ATR-based stop-loss suggestion: ${adaptive_stop:.2f}" if adaptive_stop else
                            f"\n### GLOBAL SCAN CONTEXT\n"
                            f"Region: {region} | Risk: {risk_env}({risk_score:+.2f}) | VIX: {vix_val:.1f}\n"
                            f"Position size auto-scaled to {scaled_risk:.2f}% of portfolio (VIX adjustment)."
                        )
                        full_macro_ctx = macro_ctx_str + global_note

                        signal = await loop.run_in_executor(
                            None,
                            lambda: ai.analyze_stock(
                                ai_provider, api_key, sym, quote,
                                indicators, history, news_items,
                                portfolio_ctx, full_macro_ctx,
                                rl_lessons=rl_lessons,
                                sector=sector,
                                global_context=global_ctx,
                            )
                        )
                        signal["sector"] = sector
                        if adaptive_stop and not signal.get("stop_loss"):
                            signal["stop_loss"] = adaptive_stop

                        db_signal = AISignal(
                            user_id=user.id,
                            symbol=sym,
                            signal=signal.get("signal", "HOLD"),
                            confidence=signal.get("confidence", 0),
                            target_price=signal.get("target_price"),
                            stop_loss=signal.get("stop_loss"),
                            reasoning=f"[GLOBAL SCAN/{region}] {signal.get('reasoning', '')}",
                            model_used=signal.get("model", "unknown"),
                        )
                        db.add(db_signal)
                        db.commit()

                        if signal.get("signal") in ("BUY", "COVER"):
                            # Stop-loss cooldown: 3-day ban after any [STOP-LOSS] sell
                            if _is_stop_loss_cooldown(sym, user.id, db):
                                logger.warning(f"[GlobalScan] {sym} skipped — stop-loss cooldown active (3-day ban)")
                                continue

                            # Apply VIX-scaled risk for this trade
                            set_setting(db, "risk_per_trade_pct", str(scaled_risk), user.id)
                            auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
                            set_setting(db, "risk_per_trade_pct", str(base_risk), user.id)

                            if auto_result.get("success"):
                                logger.info(
                                    f"[GlobalScan] ✅ {sym} ({region}) → BUY "
                                    f"risk={scaled_risk:.2f}% VIX={vix_val:.1f}"
                                )
                                await broadcast({
                                    "type": "auto_trade",
                                    "user": user.username,
                                    "symbol": sym,
                                    "result": auto_result,
                                    "trigger": "global_market_scan",
                                    "region": region,
                                    "vix": vix_val,
                                })
                            else:
                                logger.debug(f"[GlobalScan] {sym} BUY skipped: {auto_result.get('reason','')}")

                    except Exception as sym_e:
                        logger.error(f"[GlobalScan] Error on {sym}: {sym_e}")

        except Exception as e:
            logger.error(f"[GlobalScan] Cycle error: {e}")

        await asyncio.sleep(1200)  # Run every 20 minutes


async def background_stop_loss_monitor():
    """
    Task 11 — Stop-loss monitor.
    Runs every 5 minutes. Checks all live Alpaca positions for unrealized loss
    exceeding the stop_loss_pct threshold (default -5%). Sells the full position
    immediately when triggered. Also syncs local DB after each check.
    """
    await asyncio.sleep(180)  # 3 min startup delay
    while True:
        try:
            db = next(get_db())
            try:
                users = db.query(User).all()
                for user in users:
                    auto_trade_enabled = get_setting(db, "auto_trade_enabled", user.id, "false") == "true"
                    if not auto_trade_enabled:
                        continue

                    engine = TradingEngine(db, user.id)
                    if not engine.alpaca:
                        continue

                    stop_loss_pct = float(get_setting(db, "stop_loss_pct", user.id, "5.0"))

                    # Always sync DB with Alpaca reality
                    engine.sync_positions_from_alpaca()

                    try:
                        alpaca_positions = engine.alpaca.list_positions()
                    except Exception as e:
                        logger.error(f"[StopLoss] Cannot fetch Alpaca positions for {user.username}: {e}")
                        continue

                    # Build set of symbols already covered by an open sell order
                    try:
                        open_orders = engine.alpaca.list_orders(status="open")
                        pending_sells = {o.symbol for o in open_orders if o.side == "sell"}
                    except Exception:
                        pending_sells = set()

                    for ap in alpaca_positions:
                        symbol = ap.symbol
                        loss_pct = float(ap.unrealized_plpc) * 100
                        curr_price = float(ap.current_price)
                        total_qty = float(ap.qty)
                        # qty_available = total - qty locked in open orders for this symbol
                        locked_qty = sum(
                            float(o.qty) for o in open_orders
                            if o.symbol == symbol and o.side == "sell"
                        )
                        qty_available = max(0.0, total_qty - locked_qty)

                        if loss_pct < -stop_loss_pct:
                            # Skip if an open sell order already covers this position
                            if symbol in pending_sells:
                                logger.info(
                                    f"[StopLoss] {symbol} loss {loss_pct:.2f}% triggered but "
                                    f"a sell order is already pending — skipping duplicate"
                                )
                                continue

                            if qty_available < 0.0001:
                                logger.info(f"[StopLoss] {symbol} qty_available too small ({qty_available}), skipping")
                                continue

                            logger.warning(
                                f"[StopLoss] {symbol} TRIGGERED: {loss_pct:.2f}% "
                                f"(threshold -{stop_loss_pct}%) — selling {qty_available:.4f} available shares @ ${curr_price:.2f}"
                            )
                            result = engine.execute_sell(
                                symbol, qty_available, curr_price,
                                ai_triggered=True,
                                confidence=1.0,
                                reasoning=(
                                    f"[STOP-LOSS] Unrealized loss {loss_pct:.2f}% exceeded "
                                    f"-{stop_loss_pct}% threshold. Auto-liquidating to protect capital."
                                ),
                            )
                            if result.get("success"):
                                logger.info(f"[StopLoss] {symbol} sell order placed. Approx P&L: ${float(ap.unrealized_pl):.2f}")
                                await broadcast({
                                    "type": "stop_loss_triggered",
                                    "symbol": symbol,
                                    "loss_pct": round(loss_pct, 2),
                                    "price": curr_price,
                                    "qty": qty_available,
                                })
                            else:
                                logger.error(f"[StopLoss] {symbol} sell failed: {result}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[StopLoss] Monitor error: {e}")
        await asyncio.sleep(300)  # check every 5 minutes


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8888, reload=True)
