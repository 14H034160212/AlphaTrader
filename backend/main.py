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
import hk_ipo_scanner as hk_ipo
import tax_reporter as tax
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
    # Seed scenario lifecycle table (after tables exist, before background loops)
    try:
        from scenario_lifecycle import seed_scenarios_from_hardcoded
        from database import SessionLocal
        _seed_db = SessionLocal()
        seed_scenarios_from_hardcoded(_seed_db)
        _seed_db.close()
    except Exception as e:
        logger.warning(f"[ScenarioLifecycle] Seed failed: {e}")
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
    task13 = asyncio.create_task(background_dca_core_etf())
    task14 = asyncio.create_task(background_one_shot_rebalance())
    task15 = asyncio.create_task(background_hk_ipo_scan())
    task16 = asyncio.create_task(background_deposit_handler())
    task17 = asyncio.create_task(background_annual_tax_report())
    task18 = asyncio.create_task(background_rl_pipeline())
    task19 = asyncio.create_task(background_llm_shootout_loop())
    task20 = asyncio.create_task(background_llm_catalyst_loop())
    task21 = asyncio.create_task(background_dynamic_watchlist_loop())
    logger.info("Background tasks started: price_refresh + auto_trade_loop + event_scan + news_scan + social_sentiment + blog_monitor + kronos_gpu + daily_digest + pending_trade_executor + email_reporter + email_reply_checker + stop_loss_monitor + global_market_scan + dca_core_etf + one_shot_rebalance + hk_ipo_scan + deposit_handler + annual_tax_report + rl_policy_trainer + llm_shootout + llm_catalyst + dynamic_watchlist")
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
    task13.cancel()
    task14.cancel()
    task15.cancel()
    task16.cancel()
    task17.cancel()
    task18.cancel()
    task19.cancel()
    task20.cancel()
    task21.cancel()
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
    # Filter qty>0 only — stale rows with qty=0 + avg_cost intact (from old
    # SIMULATE trades or fully-closed positions) confuse the AI: it reads the
    # avg_cost as "currently holding at that price". Saw 0700.HK qty=0
    # avg=$462.20 listed → AI said "portfolio already holds this stock at $462.20".
    raw_positions = summary.get("positions", [])
    positions = [p for p in raw_positions if abs(float(p.get("quantity", 0) or 0)) > 0.001]
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
    # Cap at 7 days — older SIMULATE-mode paper trades (broker=Paper, before
    # REAL switch on 2026-05-15) leaked into AI prompts and produced "I'm
    # already holding 0700.HK at $462" hallucinations. Also exclude broker='Paper'
    # entries: real Alpaca/Futu fills are tagged with their broker name in the
    # broker column.
    cutoff_7d = datetime.utcnow() - timedelta(days=7)
    recent_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.timestamp >= cutoff_7d,
        Trade.broker != "Paper",
    ).order_by(Trade.timestamp.desc()).limit(15).all()

    if recent_trades:
        lines.append("\n### Recent Trade History (last 7 days, live fills only)")
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
        lines.append("\n### Recent Trade History: No live fills in last 7 days")

    # ── Performance Note ──────────────────────────────────────────────────────
    today_trades = [t for t in recent_trades if t.timestamp and
                    (datetime.utcnow() - t.timestamp).total_seconds() < 86400]
    lines.append(f"\n### Session Stats")
    lines.append(f"- Trades today: {len(today_trades)}")
    lines.append(f"- Total trades on record: {len(recent_trades)}")

    # ── HK / Futu account (SEPARATE FROM ALPACA) ─────────────────────────────
    # Without this, AI evaluating .HK symbols sees Alpaca's "cash 4%" and refuses
    # to buy, even though Moomoo HK has HK$4,656 sitting idle. Was producing 12
    # consecutive HK signals all at conf=0.72 with "cash critically low" reasoning.
    if getattr(engine, "_futu", None) and engine._futu.is_connected():
        try:
            hk_acc = engine._futu.get_account()
            hk_positions = [p for p in engine._futu.get_all_positions()
                           if p.get("market") == "HK"]
            hk_cash = float(hk_acc.get("cash", 0) or 0)
            hk_equity = float(hk_acc.get("equity", 0) or 0)
            lines.append("\n### Hong Kong Account (Moomoo — SEPARATE from US Alpaca above)")
            lines.append(f"- HK Cash: HK${hk_cash:,.2f}  (this account is denominated in HKD)")
            lines.append(f"- HK Equity: HK${hk_equity:,.2f}")
            if hk_positions:
                lines.append("- HK Holdings:")
                for p in hk_positions:
                    lines.append(f"  - {p.get('symbol')}: {p.get('quantity'):g} shares "
                                 f"@ HK${p.get('avg_cost',0):.3f} "
                                 f"(now HK${p.get('current_price',0):.3f}, "
                                 f"P&L HK${p.get('unrealized_pnl',0):+.2f})")
            else:
                lines.append(f"- HK Holdings: NONE — all HK${hk_cash:,.2f} sitting idle, available for HK stocks")
            lines.append(
                "- **IMPORTANT FOR AI**: When evaluating a `.HK` symbol, use the HK Cash above "
                "(not the US Alpaca cash). The two are completely separate broker accounts "
                "with independent cash pools. Cash-reserve rules ('15% target') apply per-account."
            )
        except Exception as _hke:
            lines.append(f"\n### Hong Kong Account: unavailable ({_hke})")

    # ── Cash Reserve Status (US / Alpaca) ────────────────────────────────────
    cash_status = engine.get_cash_reserve_status()
    lines.append(f"\n### Cash Reserve (US / Alpaca)")
    lines.append(f"- Cash: ${cash_status['cash']:,.2f} ({cash_status['cash_pct']:.0f}% of portfolio)")
    lines.append(f"- Target reserve: ${cash_status['target_cash']:,.2f} (15% minimum)")
    if not cash_status["healthy"]:
        lines.append(f"- ⚠️ CASH LOW: ${cash_status['shortfall']:,.2f} below target — prefer SELL over BUY to rebuild reserve")

    lines.append(
        "\nINSTRUCTION: Use this history to avoid re-buying a stock just sold at a loss, "
        "avoid over-concentrating in one sector, and factor in existing P&L when sizing positions. "
        "When cash is below the 15% reserve target, be MORE aggressive about SELL signals on losing positions "
        "and MORE selective about BUY signals — only buy for truly compelling opportunities."
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


_rl_lessons_cache: tuple = ("", 0.0)  # (text, timestamp)
_RL_LESSONS_TTL = 60.0  # seconds


def get_rl_lessons() -> str:
    """
    Build RL lessons from two sources:
    1. intelligence_attribution_report.json — catalyst/sector performance
    2. Live position P&L from the DB — profitable holdings → positive signals,
       losing holdings → negative signals (user directive 2026-05-13)

    Result is cached for 60 s to avoid repeated DB opens (called ~7× per scan).
    """
    import time
    from sqlalchemy import text as sa_text

    global _rl_lessons_cache
    cached_text, cached_at = _rl_lessons_cache
    if cached_text and (time.time() - cached_at) < _RL_LESSONS_TTL:
        return cached_text

    lines = ["### RL Feedback (Actual Portfolio Results — use to guide stock selection)"]

    # ── 1. Live position P&L as RL signal ───────────────────────────────────
    try:
        db = next(get_db())
        positions = db.execute(
            sa_text("SELECT symbol, avg_cost, current_price, quantity "
                    "FROM positions WHERE user_id=1 AND quantity > 0.001")
        ).fetchall()
        db.close()
        winners, losers = [], []
        for sym, cost, cur, qty in positions:
            if cost and cost > 0 and cur and cur > 0:
                pnl_pct = (cur - cost) / cost * 100
                if pnl_pct >= 5.0:
                    winners.append((sym, pnl_pct))
                elif pnl_pct <= -5.0:
                    losers.append((sym, pnl_pct))
        if winners:
            winners.sort(key=lambda x: x[1], reverse=True)
            lines.append("POSITIVE signals (profitable positions — consider adding/holding):")
            for sym, pnl in winners[:5]:
                lines.append(f"  + {sym}: {pnl:+.1f}% — market is rewarding this, weight UP")
        if losers:
            losers.sort(key=lambda x: x[1])
            lines.append("NEGATIVE signals (losing positions — be cautious, consider reducing):")
            for sym, pnl in losers[:5]:
                lines.append(f"  - {sym}: {pnl:+.1f}% — market is punishing this, weight DOWN")
    except Exception as e:
        logger.debug(f"[RL] Position P&L signal error: {e}")

    # ── 2. Large-cap user preference ────────────────────────────────────────
    lines.append(
        "\nUSER DIRECTIVE: Strongly prefer large, well-known tech companies "
        "(NVDA, AAPL, MSFT, AMZN, TSLA, GOOGL, META, AMD, BABA, etc.). "
        "Avoid small/obscure companies — they consistently underperform in this portfolio. "
        "Geopolitical/war scenarios (Iran-US) should NOT drive stock selection; focus on tech fundamentals."
    )

    # ── 3. Attribution report (catalyst/sector) ──────────────────────────────
    report_path = "/data/qbao775/AlphaTrader/intelligence_attribution_report.json"
    if os.path.exists(report_path):
        try:
            with open(report_path, "r") as f:
                report = json.load(f)
            macro_stats = report.get("catalyst_performance", {})
            sector_stats = report.get("sector_performance", {})
            if macro_stats:
                sorted_macros = sorted(macro_stats.items(), key=lambda x: x[1].get("avg_reward", 0), reverse=True)
                top = [f"{m}: {s.get('avg_reward', 0):+.2f}% avg ({s.get('count', 0)} signals)"
                       for m, s in sorted_macros[:3] if s.get('count', 0) > 0]
                bottom = [f"{m}: {s.get('avg_reward', 0):+.2f}% avg ({s.get('count', 0)} signals)"
                          for m, s in sorted_macros[-3:] if s.get('count', 0) > 0]
                if top:
                    lines.append("\nBest-performing catalysts:")
                    lines.extend([f"  + {t}" for t in top])
                if bottom:
                    lines.append("Worst-performing catalysts (avoid over-weighting):")
                    lines.extend([f"  - {b}" for b in bottom])
            if sector_stats:
                sorted_sectors = sorted(sector_stats.items(), key=lambda x: x[1].get("avg_reward", 0), reverse=True)
                lines.append("\nSector performance:")
                for sector, data in sorted_sectors:
                    if data.get("count", 0) > 0:
                        lines.append(f"  {sector}: {data['avg_reward']:+.2f}% avg 1d return")
        except Exception as e:
            logger.debug(f"[RL] Attribution report error: {e}")

    lines.append("\nINSTRUCTION: Use the above as a feedback loop — amplify what works, reduce what doesn't.")
    result = "\n".join(lines)
    _rl_lessons_cache = (result, time.time())
    return result


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
                except (json.JSONDecodeError, TypeError) as _wle:
                    # Was bare `except: pass` — corrupted watchlist setting
                    # would silently drop all user-added symbols from tracking.
                    logger.warning(
                        f"[PriceRefresh] Bad watchlist JSON for user {user.id} "
                        f"(falling back to position symbols only): {_wle}. "
                        f"Raw: {watchlist_json[:80]!r}"
                    )

                engine = TradingEngine(db, user.id)
                positions = engine.get_all_positions()
                symbols_to_track.update([p.symbol for p in positions])

            all_symbols = list(symbols_to_track)

            # Prioritize the names that actually drive trades — pinned/priority,
            # held positions, then the chip/focus thematic universe — so the
            # PRICE_FETCH_CAP below never starves them. The old hard `[:20]` slice
            # over a stable-ordered set meant ~180/200 watchlist names (incl.
            # ASML/VRT/NTAP/WDC/STX) NEVER got a price → were skipped every
            # auto-trade cycle (`if not quote: continue`) → never analyzed,
            # never traded. That alone disabled the whole chip strategy. 2026-05-28.
            try:
                import dynamic_watchlist as _dw
                _pri, _held = [], []
                for _u in users:
                    _pri += [s.strip() for s in get_setting(db, "priority_symbols", _u.id, "").split(",") if s.strip()]
                    _held += [p.symbol for p in TradingEngine(db, _u.id).get_all_positions()]
                _thematic = [s for t in _dw.THEMATIC_UNIVERSES.values() for s in t]
                _front, _seen = [], set()
                for _grp in (_pri, _held, _thematic, all_symbols):
                    for _s in _grp:
                        if _s in symbols_to_track and _s not in _seen:
                            _front.append(_s); _seen.add(_s)
                all_symbols = _front
            except Exception as _pe:
                logger.warning(f"[PriceRefresh] prioritization failed, using raw order: {_pe}")

            # Fetch prices in executor (non-blocking yfinance calls) - staggered to avoid rate limits
            loop = asyncio.get_event_loop()
            new_prices = {}
            # Cap covers the full chip universe + held + core ETFs. get_stock_quote
            # is itself 5-min cached, so the light 0.5s stagger is enough to stay
            # under Yahoo's rate limit on the .history endpoint.
            PRICE_FETCH_CAP = 250   # was 20 — covers the full ~206 watchlist with headroom (nothing silently dropped)
            fetched = 0
            for sym in all_symbols[:PRICE_FETCH_CAP]:
                try:
                    q = await loop.run_in_executor(None, md.get_stock_quote, sym)
                    if q:
                        new_prices[sym] = q["current"]
                        price_cache[sym] = q
                        fetched += 1
                except Exception as e:
                    logger.error(f"Error fetching {sym}: {e}")
                await asyncio.sleep(0.5)  # light stagger; quote is 5-min cached
            logger.info(f"[PriceRefresh] cached {fetched}/{min(len(all_symbols), PRICE_FETCH_CAP)} symbols "
                        f"(watchlist total {len(all_symbols)})")

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

        # Periodic cache maintenance and stats logging
        try:
            md.evict_cache()
            cache_stats = md.get_cache_stats()
            logger.info(f"[MarketDataCache] {cache_stats}")
        except Exception:
            pass

        await asyncio.sleep(300)  # Refresh every 5 min (cache handles inter-loop dedup)


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
                # 2026-06-23 CHURN FIX: in serenity mode derive the tradeable set LIVE
                # from held positions + recommended_tickers(), NOT the persisted
                # `watchlist` setting — it had bloated to 62 stale off-thesis names
                # (incl ORCL/GLD/LMT) and the loop churned them (buy then sell). Held
                # names stay (so we can manage/sell them); buys limited to Serenity's
                # current thesis. No persisted-list staleness.
                if get_setting(db, "watchlist_source", user.id, "serenity") == "serenity":
                    try:
                        import serenity_lens as _sl
                        _eng = TradingEngine(db, user.id)
                        _held = [p.symbol for p in _eng.get_all_positions() if p.quantity > 0.001]
                        # top_n=15: focus buys on Serenity's highest-conviction CPO/chip
                        # names, not the ~48-name tail (drops mega-cap noise GOOGL/MSFT/
                        # TSLA we deliberately moved away from). Held names kept regardless.
                        watchlist = list(dict.fromkeys(
                            _held + _sl.recommended_tickers(top_n=15) + _sl.nvda_downstream_extras()))
                    except Exception as _e:
                        logger.warning(f"[AutoTrade] serenity watchlist build failed, falling back: {_e}")
                        watchlist = json.loads(get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST)))
                else:
                    watchlist = json.loads(get_setting(db, "watchlist", user.id, json.dumps(md.DEFAULT_WATCHLIST)))

                # ⚠️ HK IPO priority injection (per user 2026-05-03):
                # Newly-listed HK tech tickers go to the FRONT of the scan queue
                # so the AI evaluates them first. Sector cap (5%) is enforced
                # downstream in trading_engine.auto_trade.
                hk_ipo_enabled = get_setting(db, "hk_ipo_priority_enabled", user.id, "true") == "true"
                if hk_ipo_enabled:
                    try:
                        ipo_json = get_setting(db, "hk_ipo_watchlist", 1, "[]")
                        ipo_list = json.loads(ipo_json)
                        ipo_syms = [r["symbol"] for r in ipo_list if r.get("symbol")]
                        if ipo_syms:
                            non_ipo = [s for s in watchlist if s not in ipo_syms]
                            watchlist = ipo_syms + non_ipo
                            logger.warning(
                                f"[AutoTrade] ⚠️  HK_IPO_PRIORITY: front-loaded "
                                f"{len(ipo_syms)} new HK tech IPOs into scan queue"
                            )
                    except Exception as _ipo_e:
                        logger.warning(f"[AutoTrade] HK IPO priority injection failed: {_ipo_e}")

                engine = TradingEngine(db, user.id)

                # ── Sync local DB positions with Alpaca before making any decision ──
                if engine.use_alpaca:
                    n = engine.sync_positions_from_alpaca()
                    logger.info(f"[AutoTrade] Synced {n} positions from Alpaca for {user.username}")

                # ── SCAN PRIORITIZATION (2026-05-27 fix) ──
                # With 200 watchlist symbols × ~60s each, a full cycle takes hours,
                # so names at the tail (MU/SNDK/AMD) rarely got analyzed → missed
                # the semi rally. Re-order so high-priority names scan FIRST:
                #   1. held positions (need sell monitoring)
                #   2. today's biggest movers (catch momentum / breakouts)
                #   3. thematic names (user's chip/semi/robotics directive)
                #   4. everyone else
                priority_syms = []   # defined here so the BUY-filter bypass below
                                     # never hits a NameError if the try block fails
                try:
                    held_syms = set()
                    if engine.use_alpaca:
                        try:
                            # Only count MEANINGFUL holdings (>= $1 market value).
                            # 16 of 20 "positions" were dust (<$1 fractional leftovers
                            # like NVDA/AMAT/LRCX after exits); they were front-loaded
                            # ahead of real chip names and wasted scan budget. 2026-05-28.
                            held_syms = {p.symbol for p in engine.alpaca.list_positions()
                                         if float(p.qty) > 0 and float(p.market_value) >= 1.0}
                        except Exception:
                            pass

                    # Today's movers from price cache (abs % change)
                    def _abs_chg(s):
                        q = price_cache.get(s) or {}
                        return abs(q.get("change_pct", 0) or 0)
                    movers = sorted([s for s in watchlist if _abs_chg(s) >= 4.0],
                                    key=_abs_chg, reverse=True)

                    # ABSOLUTE TOP priority: user-pinned priority_symbols (2026-05-27:
                    # memory/storage giants MU/WDC/STX/SNDK — user said 优先级最高).
                    priority_setting = get_setting(db, "priority_symbols", user.id, "MU,WDC,STX,SNDK")
                    priority_syms = [s.strip() for s in priority_setting.split(",") if s.strip()]

                    # Thematic priority names (chips/semis/robotics/GPU supply)
                    import dynamic_watchlist as _dw
                    thematic_pri = [s for t in _dw.THEMATIC_UNIVERSES.values() for s in t]

                    front = []
                    for grp in (priority_syms, list(held_syms), movers, thematic_pri):
                        for s in grp:
                            if s in watchlist and s not in front:
                                front.append(s)
                    rest = [s for s in watchlist if s not in front]
                    watchlist = front + rest
                    logger.info(f"[AutoTrade] scan priority: {len(priority_syms)} pinned ({priority_syms}) + "
                                f"{len(held_syms)} held + {len(movers)} movers (top: {movers[:3]}) + thematic")
                except Exception as _pe:
                    logger.warning(f"[AutoTrade] scan prioritization failed: {_pe}")

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
                active_macros = await loop.run_in_executor(None, lambda: ni.detect_active_macro_scenarios(hours_back=6, db=db))
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
                            # User-pinned priority names (storage/chip giants MU/WDC/STX/SNDK)
                            # are pre-vetted "buy the few best" picks (2026-05-27 directive:
                            # 优先级最高, 买能一直上涨的股票). They bypass the momentum-based
                            # gap filter AND the bear-market filter — the AI confidence gate
                            # (0.80) + Kelly sizing still govern the actual entry. The cooldown
                            # ban (post-stop-loss) is NOT bypassed: it remains a real safety.
                            is_priority = symbol in priority_syms

                            # Gap Filter: skip BUY if stock already up >3% today
                            if action == "BUY" and gap_pct > 3.0 and not is_priority:
                                skip_reason = f"gap filter ({gap_pct:.1f}% up today)"

                            # Bear Market Filter: suppress BUY when SPY < MA20
                            elif action == "BUY" and spy_bear_market and not is_priority:
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

                            if is_priority and action == "BUY" and gap_pct > 3.0 and not skip_reason:
                                logger.info(f"[AutoTrade] {symbol} is PINNED priority — bypassing "
                                            f"gap filter ({gap_pct:.1f}% up today); AI conf gate still applies")

                            if skip_reason:
                                logger.warning(f"[AutoTrade] {symbol} {action} skipped — {skip_reason}")
                            else:
                                # Cash reserve check: if BUY and cash is low, auto-rebalance first
                                if action == "BUY":
                                    cash_status = engine.get_cash_reserve_status()
                                    if not cash_status["healthy"]:
                                        logger.warning(
                                            f"[CashReserve] Cash {cash_status['cash_pct']:.0f}% "
                                            f"< target {engine.CASH_RESERVE_PCT*100:.0f}%, "
                                            f"shortfall ${cash_status['shortfall']:.0f} — auto-rebalancing"
                                        )
                                        freed = engine.free_cash_for_opportunity(cash_status["shortfall"])
                                        if freed:
                                            for f in freed:
                                                logger.info(f"[CashReserve] Freed ${f['freed']:.0f} from {f['symbol']} ({f['pnl_pct']:+.1f}%)")
                                                await broadcast({
                                                    "type": "auto_rebalance",
                                                    "sold": f["symbol"],
                                                    "freed": f["freed"],
                                                    "pnl_pct": f["pnl_pct"],
                                                    "reason": "Cash reserve replenishment for opportunity",
                                                })

                                auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)
                                if auto_result.get("success"):
                                    logger.info(f"Auto-trade for {user.username} - {symbol}: {auto_result}")
                                    await broadcast({"type": "auto_trade", "user": user.username, "symbol": symbol, "result": auto_result})
                                else:
                                    # Was silently swallowed — a BUY/SELL that passed every
                                    # pre-filter but got rejected inside auto_trade (sizing,
                                    # concentration cap, buying-power, focus gate, RL veto…)
                                    # left NO log, so we could not tell why chip names never
                                    # executed. Surface it. 2026-05-28.
                                    _why = auto_result.get("reason") or auto_result.get("error") or auto_result
                                    logger.warning(f"[AutoTrade] {symbol} {action} NOT executed (conf {confidence:.0%}): {_why}")
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
                except (json.JSONDecodeError, TypeError) as _wle:
                    # Was bare `except: pass` — sibling of the same bug fixed
                    # in price_refresh loop. Corrupt watchlist setting would
                    # silently drop user-added symbols from sentiment scan too.
                    logger.warning(
                        f"[SocialScan] Bad watchlist JSON for user {user.id}: {_wle}. "
                        f"Raw: {wl[:80]!r}"
                    )
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

        await asyncio.sleep(1200)  # Run every 20 minutes (reduced from 15 min)


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

                # ── Geopolitical Macro Scan + Scenario Lifecycle ───────────
                # Fetch geo news ONCE, reuse for all consumers below
                geo_news = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ni.fetch_geopolitical_news(hours_back=6)
                )

                # Run full lifecycle scan (trigger detection + resolution + decay + AI review)
                try:
                    import scenario_lifecycle as sl
                    active_macros_raw, resolution_events = sl.run_lifecycle_scan(
                        db, geo_news,
                        ai_provider=ai_provider, api_key=api_key,
                    )
                    # Use lifecycle results as active macros (already filtered to ACTIVE/DECLINING)
                    active_macros = active_macros_raw if active_macros_raw else sl.get_active_scenarios(db)
                    if resolution_events:
                        for rev in resolution_events:
                            logger.warning(
                                f"[ScenarioLifecycle] {rev['action']}: {rev['scenario_id']} "
                                f"(evidence: {rev.get('resolution_evidence_count', 0)}x)"
                            )
                            await broadcast({
                                "type": "scenario_lifecycle",
                                "action": rev["action"],
                                "scenario_id": rev["scenario_id"],
                                "evidence": rev.get("evidence", []),
                            })
                except Exception as _lce:
                    logger.error(f"[ScenarioLifecycle] Error in lifecycle scan: {_lce}")
                    active_macros = ni.detect_active_macro_scenarios(hours_back=3)

                # Auto-expand watchlist based on active scenarios and news keywords
                try:
                    new_tickers, reason = ni.get_watchlist_additions(
                        active_macros, geo_news, watchlist,
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

                                # Cash reserve check: free up cash if needed
                                cash_status = engine.get_cash_reserve_status()
                                if not cash_status["healthy"]:
                                    engine.free_cash_for_opportunity(cash_status["shortfall"])

                                # Apply VIX + scenario health scaling to position size
                                base_risk = float(get_setting(db, "risk_per_trade_pct", user.id, "2.0"))
                                scaled_risk = ps.vix_position_scale(geo_vix, base_risk)
                                scaled_risk = ps.scenario_position_scale(scaled_risk, scenario_mult)
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

                # ── Self-Restructuring / Layoff Catalyst Scan ────────────────
                # When a company announces its OWN layoffs, that is a BULLISH
                # signal (cost reduction → margin expansion). Scan all global
                # tech companies: US, China ADR, Japan, Korea, India, SE Asia.
                all_scan_syms = list({
                    s
                    for bucket in [
                        "US_TECH", "US_ENTERPRISE", "GLOBAL_TECH_ADR",
                        "US_FINANCE", "US_ENERGY",
                        "HK", "JP", "KR", "IN",
                    ]
                    for s in md.GLOBAL_POPULAR_STOCKS.get(bucket, [])
                })
                restructuring_hits = ni.detect_restructuring_catalysts(all_scan_syms, hours_back=48)
                _seen_restr = getattr(background_news_scan, "_seen_restr_headlines", set())
                new_restr = [r for r in restructuring_hits if r["headline"] not in _seen_restr]

                for hit in new_restr:
                    sym = hit["symbol"]
                    strength = hit["strength"]  # 1=minor, 2=explicit cuts, 3=large-scale
                    try:
                        logger.warning(
                            f"[Restructuring] 🔄 {sym} — layoff/restructuring detected "
                            f"(strength={strength}/3): {hit['headline'][:80]}"
                        )
                        quote = price_cache.get(sym) or md.get_stock_quote(sym)
                        if not quote:
                            continue
                        # Skip if stock already spiked >8% today (news fully priced in)
                        gap = quote.get("change_pct", 0)
                        if gap > 8.0:
                            logger.info(f"[Restructuring] {sym} already up {gap:.1f}% — skip (fully priced in)")
                            continue
                        if _is_stop_loss_cooldown(sym, user.id, db):
                            logger.info(f"[Restructuring] {sym} in stop-loss cooldown — skip")
                            continue

                        indicators = await loop.run_in_executor(None, md.get_technical_indicators, sym)
                        engine = TradingEngine(db, user.id)
                        base_risk = float(get_setting(db, "risk_per_trade_pct", user.id, "2.0"))
                        orig_min_conf = get_setting(db, "auto_trade_min_confidence", user.id, "0.75")
                        vix_now = (await loop.run_in_executor(None, gc.build_global_context)).get("vix", {}).get("value", 20)
                        scaled_risk = ps.vix_position_scale(vix_now, base_risk)

                        if strength >= 2:
                            # Strength 2-3: restructuring pattern is well-established —
                            # override AI hesitation with a direct BUY signal.
                            # Confidence: 0.82 for strength=2, 0.90 for strength=3.
                            forced_conf = 0.90 if strength == 3 else 0.82
                            atr = (indicators or {}).get("atr14", 0)
                            cur_price = quote.get("current", 0)
                            signal = {
                                "symbol": sym,
                                "signal": "BUY",
                                "confidence": forced_conf,
                                "reasoning": (
                                    f"[RESTRUCTURING AUTO-BUY strength={strength}/3] "
                                    f"{hit['headline'][:100]} — "
                                    f"Cost-cutting catalyst: layoffs historically bullish for announcing company. "
                                    f"Gap today: {gap:+.1f}%. Direct execution, no AI wait."
                                ),
                                "stop_loss": ps.atr_stop_loss(cur_price, atr) if cur_price and atr else round(cur_price * 0.95, 2),
                                "sector": ni.get_symbol_sector(sym),
                            }
                            logger.warning(
                                f"[Restructuring] ⚡ DIRECT BUY {sym} — strength={strength}, conf={forced_conf}"
                            )
                        else:
                            # Strength 1: run AI analysis with restructuring context
                            history = await loop.run_in_executor(None, md.get_stock_history, sym, "1mo")
                            news_items = await loop.run_in_executor(None, md.get_stock_news, sym)
                            portfolio_ctx = build_rich_portfolio_context(db, user.id, engine)
                            sector = ni.get_symbol_sector(sym)
                            signal = await loop.run_in_executor(
                                None,
                                lambda: ai.analyze_stock(
                                    ai_provider, api_key, sym, quote,
                                    indicators, history, news_items,
                                    portfolio_ctx, hit["context"],
                                    rl_lessons=rl_lessons,
                                    sector=sector,
                                    global_context=gc.build_global_context(),
                                )
                            )
                            signal["sector"] = ni.get_symbol_sector(sym)

                        # Record signal to DB
                        db_signal = AISignal(
                            user_id=user.id,
                            symbol=sym,
                            signal=signal.get("signal", "HOLD"),
                            confidence=signal.get("confidence", 0),
                            target_price=signal.get("target_price"),
                            stop_loss=signal.get("stop_loss"),
                            reasoning=f"[RESTRUCTURING] {signal.get('reasoning', '')}",
                            model_used=signal.get("model", f"restructuring-s{strength}"),
                        )
                        db.add(db_signal)
                        db.commit()

                        if signal.get("signal") in ("BUY", "COVER"):
                            # For strength>=2 lower the confidence gate to match forced signal
                            exec_min_conf = "0.65" if strength == 1 else str(signal["confidence"] - 0.01)
                            set_setting(db, "auto_trade_min_confidence", exec_min_conf, user.id)
                            set_setting(db, "risk_per_trade_pct", str(scaled_risk), user.id)

                            auto_result = engine.auto_trade(signal, quote["current"], indicators=indicators)

                            set_setting(db, "risk_per_trade_pct", str(base_risk), user.id)
                            set_setting(db, "auto_trade_min_confidence", orig_min_conf, user.id)

                            if auto_result.get("success"):
                                logger.info(
                                    f"[Restructuring] ✅ {sym} → BUY executed "
                                    f"strength={strength} risk={scaled_risk:.2f}% VIX={vix_now:.1f}"
                                )
                                await broadcast({
                                    "type": "auto_trade",
                                    "user": user.username,
                                    "symbol": sym,
                                    "result": auto_result,
                                    "trigger": "restructuring_catalyst",
                                    "strength": strength,
                                    "headline": hit["headline"],
                                })
                            else:
                                # Restore settings even on failure
                                set_setting(db, "risk_per_trade_pct", str(base_risk), user.id)
                                set_setting(db, "auto_trade_min_confidence", orig_min_conf, user.id)
                                logger.info(f"[Restructuring] {sym} BUY skipped: {auto_result.get('reason','')}")

                    except Exception as re:
                        logger.error(f"[Restructuring] Error on {sym}: {re}")
                    _seen_restr.add(hit["headline"])

                background_news_scan._seen_restr_headlines = _seen_restr

                # Also backfill RL outcomes once per day (run at ~midnight UTC)
                if datetime.utcnow().hour == 0 and datetime.utcnow().minute < 10:
                    rl.update_trade_outcomes()
                    _run_daily_maintenance(db)

        except Exception as e:
            logger.error(f"[NewsScan] Loop error: {e}")

        await asyncio.sleep(900)  # Every 15 minutes (reduced from 10 min to lower CPU)


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
                    sf.write(f"=== SerenityAlphaTrader Log Rotation Summary ({datetime.utcnow().isoformat()}) ===\n")
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


@app.get("/api/scenarios")
async def get_scenarios(db: Session = Depends(get_db)):
    """
    Return all macro scenario lifecycle states (ACTIVE, DECLINING, RESOLVED, EXPIRED).
    Includes AI-generated scenarios, resolution evidence, and health metadata.
    """
    try:
        import scenario_lifecycle as sl
        return sl.get_all_scenarios(db)
    except Exception as e:
        logger.error(f"[Scenarios API] Error: {e}")
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


@app.get("/api/tax/nz-summary")
async def get_nz_tax_summary(
    fy_ending_year: int,
    format: str = "json",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """NZ tax-year summary for the given financial year (ending March of fy_ending_year).
    Example: fy_ending_year=2027 → period 2026-04-01 to 2027-03-31.
    Hand the CSV output to your chartered accountant for IR3 filing.

    Query params:
      fy_ending_year — required; the calendar year the NZ FY ends in
      format         — 'json' (default), 'csv', or 'html'
    """
    engine = TradingEngine(db, current_user.id)
    if not engine.alpaca:
        raise HTTPException(status_code=400, detail="Alpaca not configured for this user")
    summary = await asyncio.get_event_loop().run_in_executor(
        None, tax.compute_summary, engine.alpaca, fy_ending_year
    )
    if format == "csv":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            content=tax.to_csv_bundle(summary),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="alphatrader_nz_tax_fy{fy_ending_year}.csv"'
            },
        )
    if format == "html":
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=tax.to_html(summary))
    return summary


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
            
            msg = f"💼 **SerenityAlphaTrader Portfolio ({summary['provider']})**\n\n"
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
        return {"response": f"⚠️ SerenityAlphaTrader Error: {str(e)}"}


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
    sent = er.send_email(sender, app_pw, recipient, "SerenityAlphaTrader — Test Email", html)
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
# RL Pipeline API
# ─────────────────────────────────────────────

@app.get("/api/rl/pipeline")
async def rl_pipeline_status():
    """Current pipeline state: production version, shadow, recent runs."""
    import rl_pipeline as _pipe
    return _pipe.get_status()


@app.post("/api/rl/pipeline/run")
async def rl_pipeline_run_now(background_tasks: BackgroundTasks):
    """Manually trigger one pipeline cycle (runs in background)."""
    import rl_pipeline as _pipe
    background_tasks.add_task(lambda: _pipe.run_cycle())
    return {"status": "triggered", "message": "Pipeline cycle running in background"}


@app.get("/api/rl/models")
async def rl_models_registry():
    """Full version registry — every candidate trained, with metrics + decision."""
    import rl_validation as _val
    return _val.load_registry()


@app.post("/api/rl/promote/{version}")
async def rl_promote_version(version: str):
    """
    Manually promote a saved version to production.  Useful for rollback or
    overriding the auto-promotion logic.
    """
    import rl_validation as _val
    import rl_policy_model as _rlpm
    import shutil
    registry = _val.load_registry()
    versioned_path = os.path.join(_rlpm.MODELS_DIR, f"xgb_{version}.pkl")
    if not os.path.exists(versioned_path):
        raise HTTPException(404, f"Version {version} not found")
    shutil.copyfile(versioned_path, _rlpm.MODEL_FILE)
    registry["production"] = version
    _val.save_registry(registry)
    return {"status": "promoted", "version": version}


@app.post("/api/rl/promote-shadow")
async def rl_promote_shadow():
    """Promote the current shadow model to production."""
    import rl_validation as _val
    import rl_policy_model as _rlpm
    registry = _val.load_registry()
    shadow_version = registry.get("shadow")
    if not shadow_version or not os.path.exists(_rlpm.SHADOW_FILE):
        raise HTTPException(404, "No shadow model to promote")
    import shutil
    shutil.copyfile(_rlpm.SHADOW_FILE, _rlpm.MODEL_FILE)
    registry["production"] = shadow_version
    registry["shadow"]     = None
    _val.save_registry(registry)
    _rlpm.remove_shadow()
    return {"status": "promoted", "version": shadow_version}


@app.post("/api/rl/shootout")
async def rl_model_shootout(test_set: str = "challenge",
                             max_samples: int = 100,
                             background_tasks: BackgroundTasks = None):
    """
    Head-to-head model comparison on the same test set.
    Auto-detects every reachable backend (Ollama models + LoRA vLLM) and
    scores each with the same BUY/SELL/HOLD decision rule.

    test_set: "challenge" | "holdout" | "combined"
    Heavy: takes 10-30 min depending on # of backends and samples.
    Runs in the background — poll /api/rl/shootout/latest for results.
    """
    import rl_raw_model_validator as _raw
    if background_tasks is not None:
        background_tasks.add_task(_raw.run_baseline_shootout, test_set, max_samples)
        return {"status": "started",
                "message": f"Shootout queued on {test_set} test set (~10-30 min)",
                "poll_endpoint": "/api/rl/shootout/latest"}
    return _raw.run_baseline_shootout(test_set=test_set, max_samples=max_samples)


@app.get("/api/rl/shootout/latest")
async def rl_shootout_latest():
    """Return the most recent shootout report from rl_models/raw_model_reports/."""
    import rl_raw_model_validator as _raw
    if not os.path.exists(_raw.RAW_REPORT_DIR):
        return {"status": "no_reports"}
    files = sorted(os.listdir(_raw.RAW_REPORT_DIR), reverse=True)
    if not files:
        return {"status": "no_reports"}
    with open(os.path.join(_raw.RAW_REPORT_DIR, files[0])) as f:
        return json.load(f)


@app.get("/api/rl/challenge")
async def rl_challenge_status():
    """Status of the permanent challenge test set (hard examples)."""
    import rl_challenge_set as _cs
    return _cs.get_status()


@app.get("/api/rl/errors")
async def rl_error_analysis():
    """Production-system error patterns by sector / action / confidence."""
    import rl_data_collector as _rl
    import rl_challenge_set as _cs
    records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
    return _cs.analyze_errors(records)


@app.post("/api/rl/challenge/mine")
async def rl_challenge_mine(max_new: int = 100):
    """Manually trigger hard-example mining (also runs every pipeline cycle)."""
    import rl_data_collector as _rl
    import rl_challenge_set as _cs
    records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
    return _cs.mine_hard_examples(records, max_new=max_new)


@app.get("/api/rl/compare")
async def rl_compare(holdout_days: int = 7, include_lora: bool = False):
    """
    Apples-to-apples comparison of every available method on the same holdout.
    Each method scored by the SAME unified decision rule (BUY/SELL/HOLD →
    win/loss against realised reward_3d).  include_lora=true takes ~10 min.
    """
    import rl_compare as _cmp
    return _cmp.run_comparison(holdout_days=holdout_days, include_lora=include_lora)


@app.post("/api/rl/lora/deploy")
async def rl_lora_deploy_manual():
    """Manually deploy the current adapter (skips auto-deploy gate)."""
    import rl_lora_deploy as _dep
    # __file__ is in module scope, accessible from within this function.
    # The `"__file__" in dir()` check used to fail (dir() returns local names)
    # so the fallback hardcoded path always won — fixed by referencing it
    # directly through module scope.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    adapter_dir = os.path.join(repo_root, "training", "lora_checkpoints", "best")
    if not os.path.exists(adapter_dir):
        raise HTTPException(404, "No LoRA adapter at training/lora_checkpoints/best/")
    version = datetime.utcnow().strftime("v%Y%m%d_%H%M%S")
    result = _dep.deploy_adapter(adapter_dir, version=version)
    return result


@app.post("/api/rl/lora/rollback")
async def rl_lora_rollback():
    """Stop the LoRA vLLM service and clear the routing setting."""
    import rl_lora_deploy as _dep
    return _dep.rollback_lora()


@app.post("/api/rl/lora/auto-deploy/{enabled}")
async def rl_lora_set_auto_deploy(enabled: str):
    """Toggle auto-deployment of LoRA models that pass validation."""
    val = "true" if enabled.lower() in ("true", "1", "on", "yes") else "false"
    db = next(get_db())
    try:
        set_setting(db, "lora_auto_deploy_enabled", val, 1)
    finally:
        db.close()
    return {"lora_auto_deploy_enabled": val}


@app.get("/api/rl/lora/status")
async def rl_lora_status():
    """LoRA service status: vLLM running? deployed adapter? auto-deploy toggle?"""
    import rl_lora_deploy as _dep
    db = next(get_db())
    try:
        url        = get_setting(db, "lora_inference_url",        1, "")
        version    = get_setting(db, "lora_model_version",        1, "")
        deployed_at= get_setting(db, "lora_deployed_at",          1, "")
        auto       = get_setting(db, "lora_auto_deploy_enabled",  1, "false")
    finally:
        db.close()
    return {
        "vllm_url":             url,
        "deployed_version":     version,
        "deployed_at":          deployed_at,
        "auto_deploy_enabled":  auto == "true",
        "vllm_port_in_use":     _dep._is_port_in_use(_dep.LORA_VLLM_PORT),
    }


@app.get("/api/rl/shadow/comparison")
async def rl_shadow_comparison(days: int = 7):
    """
    A/B comparison of shadow vs production predictions over the last N days,
    based on records that already have realised reward_3d.
    """
    import rl_data_collector as _rl
    records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
    cutoff  = datetime.utcnow() - timedelta(days=days)

    prod_hits, prod_total = 0, 0
    shadow_hits, shadow_total = 0, 0
    prod_rmse_sum, shadow_rmse_sum = 0.0, 0.0
    import math as _m
    for r in records:
        reward = r.get("reward_3d")
        if reward is None or not isinstance(reward, (int, float)) or not _m.isfinite(reward):
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)   # cutoff is tz-naive
        except Exception:
            continue
        if ts < cutoff:
            continue

        prod_score   = r.get("rl_policy_score")
        shadow_score = r.get("rl_shadow_score")
        if abs(reward) < 0.5:    # ignore near-zero noise
            continue

        if isinstance(prod_score, (int, float)) and _m.isfinite(prod_score):
            prod_total += 1
            if (prod_score > 0) == (reward > 0):
                prod_hits += 1
            prod_rmse_sum += (prod_score - reward) ** 2
        if isinstance(shadow_score, (int, float)) and _m.isfinite(shadow_score):
            shadow_total += 1
            if (shadow_score > 0) == (reward > 0):
                shadow_hits += 1
            shadow_rmse_sum += (shadow_score - reward) ** 2

    def _stat(hits, total, rmse_sum):
        return {
            "samples": total,
            "directional_accuracy": round(hits / total * 100, 2) if total else None,
            "rmse": round((rmse_sum / total) ** 0.5, 4) if total else None,
        }

    return {
        "window_days": days,
        "production":  _stat(prod_hits,   prod_total,   prod_rmse_sum),
        "shadow":      _stat(shadow_hits, shadow_total, shadow_rmse_sum),
    }


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
                            equity_v = float(acct.equity)
                            net_deposits = engine.get_alpaca_net_deposits()
                            if net_deposits is None or net_deposits <= 0:
                                net_deposits = float(get_setting(db, "initial_cash", 1, "100000.0"))
                            inception_pnl = equity_v - net_deposits
                            inception_pct = (inception_pnl / net_deposits * 100) if net_deposits > 0 else 0
                            alpaca_account = {
                                "equity": equity_v,
                                "cash": float(acct.cash),
                                "unrealized_pl": equity_v - float(acct.last_equity),
                                "initial_cash": net_deposits,
                                "inception_pnl": inception_pnl,
                                "inception_pct": inception_pct,
                                "core_target_pct": float(get_setting(db, "core_target_pct", 1, "50.0")),
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
                            None, lambda: ni_local.detect_active_macro_scenarios(hours_back=4)
                        )
                        for s in (scenarios or []):
                            macro_scenarios.append({
                                "name": s.get("name", ""),
                                "severity": s.get("severity", "LOW"),
                                "beneficiaries": s.get("potential_beneficiaries", []),
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
                    subject = f"SerenityAlphaTrader Daily Report — {now.strftime('%Y-%m-%d')}"
                    sent = er.send_email(sender, app_pw, recipient, subject, html)
                    if sent:
                        last_sent_date = now.date()
                        logger.info(f"[EmailReport] Daily report sent for {now.date()}")

                    # ── Separate HK report ────────────────────────────────────
                    # Sent only when Futu is enabled + connected. Standalone email
                    # so the user can glance at HK P&L on phone without scrolling
                    # past the (US-heavy) main report.
                    try:
                        if engine._futu and engine._futu.is_connected():
                            hk_account = engine._futu.get_account()  # HKD-denominated
                            hk_positions = [
                                p for p in engine._futu.get_all_positions()
                                if p.get("market") == "HK"
                            ]
                            since_24h = datetime.utcnow() - timedelta(hours=24)
                            hk_signals = [
                                {
                                    "symbol": s.symbol, "signal": s.signal,
                                    "confidence": s.confidence,
                                    "reasoning": s.reasoning or "",
                                    "timestamp": s.timestamp.isoformat() if s.timestamp else "",
                                }
                                for s in db.query(AISignal)
                                    .filter(AISignal.user_id == 1,
                                            AISignal.timestamp >= since_24h,
                                            AISignal.symbol.like("%.HK"))
                                    .order_by(AISignal.timestamp.desc())
                                    .limit(20)
                                    .all()
                            ]
                            hk_trades_today = [
                                {
                                    "symbol": t.symbol, "side": t.side,
                                    "quantity": t.quantity, "price": t.price,
                                    "total_value": t.total_value,
                                    "timestamp": t.timestamp.isoformat() if t.timestamp else "",
                                }
                                for t in db.query(Trade)
                                    .filter(Trade.user_id == 1,
                                            Trade.timestamp >= since_24h,
                                            Trade.symbol.like("%.HK"))
                                    .order_by(Trade.timestamp.desc())
                                    .all()
                            ]
                            hk_html = er.generate_hk_report_html(
                                date_str, hk_account, hk_positions,
                                hk_signals, hk_trades_today,
                            )
                            hk_subject = f"🇭🇰 SerenityAlphaTrader HK Daily — {now.strftime('%Y-%m-%d')}"
                            er.send_email(sender, app_pw, recipient, hk_subject, hk_html)
                            logger.info(f"[EmailReport] HK report sent ({len(hk_positions)} positions, "
                                        f"{len(hk_trades_today)} trades, {len(hk_signals)} signals)")
                    except Exception as _hke:
                        logger.warning(f"[EmailReport] HK report send failed: {_hke}")
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
                    "US":         ["US_TECH", "US_ENTERPRISE", "GLOBAL_TECH_ADR", "US_FINANCE", "US_ENERGY"],
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


def _load_position_highs() -> dict:
    """High-water-mark per symbol for trailing stops. Persisted to JSON."""
    import json as _json
    try:
        with open("/data/qbao775/AlphaTrader/.position_highs.json") as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_position_highs(highs: dict) -> None:
    import json as _json
    try:
        with open("/data/qbao775/AlphaTrader/.position_highs.json", "w") as f:
            _json.dump(highs, f)
    except Exception:
        pass


async def background_stop_loss_monitor():
    """
    Task 11 — Trailing stop-loss monitor (long-term-investor mode, 2026-05-26).

    Runs every 5 minutes. Uses a TRAILING stop from each position's high-water
    mark (peak price since purchase) rather than a fixed entry-based stop. This
    lets winners run while locking in gains:
      • trailing_stop_pct (default 15%): sell if price drops 15% below its peak
      • catastrophic floor (default 20% from ENTRY): hard exit regardless of peak
      • core ETFs (SPY/VOO/QQQ) never auto-stopped
    The wide 15% trailing band (vs old 5% fixed) stops the over-trading the user
    flagged — quality names ride normal volatility, only exit on real breakdowns.
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
                    trailing_stop_pct = float(get_setting(db, "trailing_stop_pct", user.id, "15.0"))
                    catastrophic_pct = float(get_setting(db, "catastrophic_stop_pct", user.id, "20.0"))
                    position_highs = _load_position_highs()

                    # Always sync DB with Alpaca reality
                    engine.sync_positions_from_alpaca()

                    try:
                        alpaca_positions = engine.alpaca.list_positions()
                    except Exception as e:
                        logger.error(f"[StopLoss] Cannot fetch Alpaca positions for {user.username}: {e}")
                        continue

                    # Build set of symbols already covered by an open sell order
                    open_orders = []
                    pending_sells = set()
                    try:
                        open_orders = engine.alpaca.list_orders(status="open")
                        pending_sells = {o.symbol for o in open_orders if o.side == "sell"}
                    except Exception:
                        pass

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

                        # Long-term core ETFs (SPY/VOO/QQQ) are protected — never
                        # auto-stopped. User decides exit manually.
                        if ps.is_core_etf(symbol):
                            continue

                        # ── TRAILING STOP from high-water mark (2026-05-26) ──
                        # Update peak, then trigger if price fell trailing_stop_pct
                        # below it. Two independent exits:
                        #   1. trailing: price < peak × (1 - trailing_stop_pct/100)
                        #   2. catastrophic: loss from entry < -catastrophic_pct
                        avg_entry = float(ap.avg_entry_price)
                        prev_peak = position_highs.get(symbol, avg_entry)
                        peak = max(prev_peak, curr_price, avg_entry)
                        position_highs[symbol] = peak   # persist updated peak

                        drawdown_from_peak = (curr_price - peak) / peak * 100 if peak > 0 else 0
                        trailing_triggered = drawdown_from_peak < -trailing_stop_pct
                        catastrophic_triggered = loss_pct < -catastrophic_pct

                        if trailing_triggered or catastrophic_triggered:
                            trigger_kind = ("CATASTROPHIC" if catastrophic_triggered
                                            else "TRAILING")
                            adaptive_threshold = (catastrophic_pct if catastrophic_triggered
                                                  else trailing_stop_pct)
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
                                f"[StopLoss] {symbol} {trigger_kind} TRIGGERED: "
                                f"price ${curr_price:.2f}, peak ${peak:.2f} "
                                f"(drawdown {drawdown_from_peak:.1f}% from peak, "
                                f"{loss_pct:.1f}% from entry) — "
                                f"selling {qty_available:.4f} shares"
                            )
                            result = engine.execute_sell(
                                symbol, qty_available, curr_price,
                                ai_triggered=True,
                                confidence=1.0,
                                reasoning=(
                                    f"[{trigger_kind} STOP] {symbol} fell {drawdown_from_peak:.1f}% "
                                    f"from peak ${peak:.2f} (entry P&L {loss_pct:.1f}%). "
                                    f"Trailing stop {trailing_stop_pct:.0f}% / catastrophic "
                                    f"{catastrophic_pct:.0f}%. Protecting capital + locked gains."
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

                    # Persist updated high-water marks for trailing stops
                    _save_position_highs(position_highs)
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[StopLoss] Monitor error: {e}")
        await asyncio.sleep(300)  # check every 5 minutes


async def background_hk_ipo_scan():
    """
    Task 15 — Recent HK tech IPO discovery (per user request, 2026-05-03).

    ⚠️  HIGH-RISK STRATEGY by user direction:
        Newly-listed HK tech stocks get top trading priority.
        Hard caps applied upstream:
          • Per-name: 3% of equity (HK_IPO_MAX_NAME_PCT)
          • Sector total: 5% of equity (HK_IPO_NEW in SECTOR_CAP_OVERRIDES)
        Disable by setting `hk_ipo_priority_enabled=false`.

    Refreshes the IPO watchlist once per hour. The list is consumed
    upstream by:
      - position_sizer.get_sector() — tags these as HK_IPO_NEW
      - background_auto_trade_loop — prioritizes these symbols first
    """
    await asyncio.sleep(180)
    last_refresh: datetime = datetime.min
    while True:
        try:
            db = next(get_db())
            try:
                enabled = get_setting(db, "hk_ipo_priority_enabled", 1, "true") == "true"
                if not enabled:
                    ps.register_hk_ipos([])
                    set_setting(db, "hk_ipo_watchlist", "[]", 1)
                    await asyncio.sleep(3600)
                    continue

                # Throttle: refresh hourly to avoid hammering AAStocks
                if (datetime.utcnow() - last_refresh).total_seconds() < 3600:
                    await asyncio.sleep(300)
                    continue

                ipos = await asyncio.get_event_loop().run_in_executor(
                    None, hk_ipo.get_recent_hk_tech_ipos
                )
                last_refresh = datetime.utcnow()

                symbols = [r["symbol"] for r in ipos]
                ps.register_hk_ipos(symbols)
                set_setting(db, "hk_ipo_watchlist", json.dumps(ipos), 1)

                if symbols:
                    logger.warning(
                        f"[HK_IPO] ⚠️  {len(symbols)} new HK tech IPOs registered "
                        f"with HIGH PRIORITY (5% sector cap, 3% per name): "
                        f"{', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}"
                    )
                else:
                    logger.info("[HK_IPO] No recent HK tech IPOs found this scan")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[HK_IPO] scan error: {e}")
        await asyncio.sleep(600)  # re-check every 10 min; refresh gated to hourly


async def background_annual_tax_report():
    """
    Task 17 — NZ tax-year summary auto-email.

    Fires once on April 1 (NZ FY just ended March 31). Generates the full
    tax summary for the just-completed FY and emails it to the user with
    the CSV attached so they can forward to their accountant.

    Idempotent: writes `tax_report_sent_fy<YYYY>` to settings; won't re-fire
    if already sent for that FY. Disable via `annual_tax_report_enabled=false`.
    """
    await asyncio.sleep(600)
    while True:
        try:
            now = datetime.utcnow()
            # Only do work in early April (NZ FY ended March 31). Window: April 1-7.
            if not (now.month == 4 and now.day <= 7):
                await asyncio.sleep(3600 * 6)  # check every 6h outside window
                continue

            db = next(get_db())
            try:
                users = db.query(User).all()
                for user in users:
                    enabled = get_setting(db, "annual_tax_report_enabled", user.id, "true") == "true"
                    if not enabled:
                        continue

                    fy_year = now.year  # FY just ended Mar 31 of this calendar year
                    flag_key = f"tax_report_sent_fy{fy_year}"
                    if get_setting(db, flag_key, user.id, "false") == "true":
                        continue

                    engine = TradingEngine(db, user.id)
                    if not engine.alpaca:
                        continue

                    settings = {s.key: s.value for s in db.query(Settings).filter_by(user_id=user.id).all()}
                    sender = settings.get("email_sender", "")
                    app_pw = settings.get("email_app_password", "")
                    recipient = settings.get("email_recipient", "")
                    if not (sender and app_pw and recipient):
                        logger.warning(f"[TaxReport] {user.username} email not configured")
                        continue

                    summary = await asyncio.get_event_loop().run_in_executor(
                        None, tax.compute_summary, engine.alpaca, fy_year
                    )
                    csv_body = tax.to_csv_bundle(summary)
                    html_body = tax.to_html(summary)

                    # Send email with CSV attached
                    sent = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: _send_tax_email(
                            sender, app_pw, recipient,
                            f"SerenityAlphaTrader NZ Tax Summary — FY {fy_year}",
                            html_body, csv_body, fy_year,
                        ),
                    )
                    if sent:
                        set_setting(db, flag_key, "true", user.id)
                        logger.warning(f"[TaxReport] ✅ FY{fy_year} summary emailed to {recipient}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[TaxReport] error: {e}")
        await asyncio.sleep(3600 * 12)  # check twice a day during window


def _send_tax_email(sender, app_pw, recipient, subject, html_body, csv_body, fy_year):
    """SMTP-send the tax summary with CSV attached. Returns True on success."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))
        attach = MIMEBase("application", "octet-stream")
        attach.set_payload(csv_body.encode("utf-8"))
        encoders.encode_base64(attach)
        attach.add_header("Content-Disposition", f'attachment; filename="alphatrader_nz_tax_fy{fy_year}.csv"')
        msg.attach(attach)
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, app_pw)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"[TaxReport] SMTP error: {e}")
        return False


async def background_deposit_handler():
    """
    Task 16 — Deposit-triggered immediate allocation (replaces monthly DCA).

    Polls Alpaca CSD/JNLC/JNLS activities every 30 minutes. When a new
    deposit is detected (id not seen before), immediately allocates
    `core_target_pct` of it to the core ETF (SPY) on the next market-open
    tick. The remainder stays as cash so the existing auto-trade loop can
    deploy it into tech satellites organically based on AI signals.

    Why this beats monthly day-1 DCA:
      • User funds ~monthly but on no fixed date — day-1 cron mistimes
      • Buys at deposit-day prices, not stale month-old prices
      • Self-paces with however often the user actually funds

    Disable via `deposit_handler_enabled=false`.
    State: `deposit_handler_last_id` tracks the latest processed CSD id.
    """
    await asyncio.sleep(240)
    while True:
        try:
            db = next(get_db())
            try:
                users = db.query(User).all()
                for user in users:
                    enabled = get_setting(db, "deposit_handler_enabled", user.id, "true") == "true"
                    if not enabled:
                        continue

                    engine = TradingEngine(db, user.id)
                    if not engine.alpaca:
                        continue

                    last_id = get_setting(db, "deposit_handler_last_id", user.id, "")
                    core_pct = float(get_setting(db, "core_target_pct", user.id, "50.0")) / 100
                    core_symbol = get_setting(db, "core_etf_symbol", user.id, "SPY").upper()

                    # Pull recent cash-deposit activities. Alpaca returns newest first.
                    new_deposits: list = []
                    try:
                        for at in ("CSD", "JNLC", "JNLS"):
                            for a in engine.alpaca.get_activities(activity_types=at):
                                if str(a.id) == last_id:
                                    raise StopIteration
                                # Only positive net amounts (deposits, not withdrawals)
                                if float(a.net_amount) > 0:
                                    new_deposits.append(a)
                    except StopIteration:
                        pass
                    except Exception as e:
                        logger.error(f"[Deposit] activities fetch failed: {e}")
                        continue

                    if not new_deposits:
                        continue

                    # Sort chronologically (oldest first) so they get processed in order
                    new_deposits.sort(key=lambda a: a.date)

                    # Check market open once per cycle
                    try:
                        clock = engine.alpaca.get_clock()
                        market_open = clock.is_open
                    except Exception:
                        market_open = False

                    if not market_open:
                        ts = new_deposits[-1].date
                        logger.info(
                            f"[Deposit] {len(new_deposits)} new deposit(s) detected (latest {ts}), "
                            f"waiting for market open to allocate"
                        )
                        continue

                    quote = md.get_stock_quote(core_symbol)
                    if not quote or not quote.get("current"):
                        logger.error(f"[Deposit] {core_symbol} quote unavailable, retry next cycle")
                        continue
                    core_price = float(quote["current"])

                    for deposit in new_deposits:
                        amount = float(deposit.net_amount)
                        deposit_id = str(deposit.id)
                        core_dollars = amount * core_pct
                        core_qty = round(core_dollars / core_price, 4)
                        if core_qty < 0.001:
                            set_setting(db, "deposit_handler_last_id", deposit_id, user.id)
                            continue

                        result = engine.execute_buy(
                            core_symbol, core_qty, core_price,
                            ai_triggered=True, confidence=1.0,
                            reasoning=(
                                f"[DEPOSIT-AUTO] {core_pct*100:.0f}% of ${amount:.2f} deposit "
                                f"auto-allocated to {core_symbol}. Remainder stays cash for "
                                f"AI to deploy into satellites."
                            ),
                        )
                        if result.get("success"):
                            set_setting(db, "deposit_handler_last_id", deposit_id, user.id)
                            logger.warning(
                                f"[Deposit] ✅ +${amount:.2f} {deposit.activity_type} "
                                f"→ bought {core_qty} {core_symbol} @ ${core_price:.2f} "
                                f"(${core_dollars:.2f}, {core_pct*100:.0f}%)"
                            )
                            await broadcast({
                                "type": "deposit_allocated",
                                "amount": amount,
                                "symbol": core_symbol,
                                "qty": core_qty,
                                "spent": core_dollars,
                            })
                        else:
                            logger.error(f"[Deposit] {core_symbol} buy failed for ${amount}: {result}")
                            break  # leave last_id unset to retry next cycle
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[Deposit] handler error: {e}")
        await asyncio.sleep(1800)  # check every 30 min


async def background_one_shot_rebalance():
    """
    Task 14 — One-shot policy rebalance executor.

    When DB setting `pending_policy_rebalance == 'true'`, on the next
    market-open tick this task:
      1. Sells every position whose sector is NOT in the satellite-tech
         whitelist (Tech/Semi/Auto/China/Crypto/Cybersecurity), keeping
         core ETFs and tech-flavored satellites untouched.
      2. Refreshes account state and buys the configured core ETF (SPY)
         until it reaches `core_target_pct`.
      3. Clears the flag and records `policy_rebalance_completed_at`.

    Idempotent on retry — sell list and SPY buy size are recomputed each
    tick from current state, so a partial run resumes safely.
    """
    SATELLITE_KEEP_SECTORS = {
        "Tech", "Semi", "Auto", "China", "Crypto", "Cybersecurity"
    }

    await asyncio.sleep(60)
    while True:
        try:
            db = next(get_db())
            try:
                users = db.query(User).all()
                for user in users:
                    pending = get_setting(db, "pending_policy_rebalance", user.id, "false")
                    if pending != "true":
                        continue

                    engine = TradingEngine(db, user.id)
                    if not engine.alpaca:
                        continue

                    try:
                        clock = engine.alpaca.get_clock()
                        if not clock.is_open:
                            continue
                    except Exception as e:
                        logger.error(f"[Rebalance] clock fetch failed: {e}")
                        continue

                    target_core_pct = float(
                        get_setting(db, "core_target_pct", user.id, "50.0")
                    ) / 100
                    core_symbol = get_setting(db, "core_etf_symbol", user.id, "SPY").upper()

                    try:
                        positions = engine.alpaca.list_positions()
                    except Exception as e:
                        logger.error(f"[Rebalance] positions fetch failed: {e}")
                        continue

                    # ── Step 1: sell non-tech satellites ─────────────────────
                    sells_done = []
                    for p in positions:
                        sym = p.symbol.upper()
                        qty = float(p.qty)
                        mv = abs(float(p.market_value))
                        sector = ps.get_sector(sym)

                        if ps.is_core_etf(sym):
                            continue
                        if mv < 1.0:
                            continue
                        if sector in SATELLITE_KEEP_SECTORS:
                            logger.info(
                                f"[Rebalance] keep {sym} (sector={sector}, mv=${mv:.2f})"
                            )
                            continue

                        cur_price = float(p.current_price)
                        result = engine.execute_sell(
                            sym, qty, cur_price,
                            ai_triggered=True, confidence=1.0,
                            reasoning=(
                                f"[POLICY-REBALANCE] Selling non-tech sector "
                                f"({sector}) to fund core {core_symbol} allocation"
                            ),
                        )
                        if result.get("success"):
                            sells_done.append({"symbol": sym, "mv": mv, "sector": sector})
                            logger.warning(
                                f"[Rebalance] SOLD {sym} qty={qty} (~${mv:.2f}, {sector})"
                            )
                            await broadcast({
                                "type": "policy_rebalance_sell",
                                "symbol": sym, "qty": qty, "mv": mv, "sector": sector,
                            })
                        else:
                            logger.error(f"[Rebalance] {sym} sell failed: {result}")

                    if sells_done:
                        await asyncio.sleep(20)  # let market orders fill

                    # ── Step 2: buy core ETF up to target ───────────────────
                    try:
                        acct = engine.alpaca.get_account()
                        positions_now = engine.alpaca.list_positions()
                    except Exception as e:
                        logger.error(f"[Rebalance] post-sell fetch failed: {e}")
                        continue

                    total_equity = float(acct.equity)
                    cash_avail = float(acct.cash)
                    existing_core_mv = sum(
                        float(p.market_value) for p in positions_now
                        if p.symbol.upper() == core_symbol
                    )
                    target_core_dollars = total_equity * target_core_pct
                    core_dollars_to_buy = max(0, target_core_dollars - existing_core_mv)
                    # Leave a tiny buffer to avoid insufficient funds rejection
                    core_dollars_to_buy = min(core_dollars_to_buy, cash_avail - 0.50)

                    if core_dollars_to_buy < 1.0:
                        logger.info(
                            f"[Rebalance] no {core_symbol} buy needed "
                            f"(existing=${existing_core_mv:.2f}, cash=${cash_avail:.2f})"
                        )
                    else:
                        quote = md.get_stock_quote(core_symbol)
                        if not quote or not quote.get("current"):
                            logger.error(f"[Rebalance] {core_symbol} quote unavailable, retry next tick")
                            continue
                        core_price = float(quote["current"])
                        core_qty = round(core_dollars_to_buy / core_price, 4)
                        if core_qty < 0.001:
                            logger.info(f"[Rebalance] {core_symbol} qty too small, skip")
                        else:
                            result = engine.execute_buy(
                                core_symbol, core_qty, core_price,
                                ai_triggered=True, confidence=1.0,
                                reasoning=(
                                    f"[POLICY-REBALANCE] Buying core ETF to "
                                    f"{target_core_pct*100:.0f}% target allocation"
                                ),
                            )
                            if result.get("success"):
                                logger.warning(
                                    f"[Rebalance] BOUGHT {core_qty} {core_symbol} "
                                    f"@ ${core_price:.2f} (~${core_dollars_to_buy:.2f})"
                                )
                                await broadcast({
                                    "type": "policy_rebalance_buy",
                                    "symbol": core_symbol, "qty": core_qty,
                                    "price": core_price, "spent": core_dollars_to_buy,
                                })
                            else:
                                logger.error(f"[Rebalance] {core_symbol} buy failed: {result}")
                                continue  # leave flag set, retry next tick

                    # ── Step 3: clear flag, log completion ──────────────────
                    set_setting(db, "pending_policy_rebalance", "false", user.id)
                    set_setting(db, "policy_rebalance_completed_at",
                                datetime.utcnow().isoformat(), user.id)
                    sells_summary = ", ".join(s["symbol"] for s in sells_done) or "none"
                    logger.warning(
                        f"[Rebalance] COMPLETE for {user.username} — sold: {sells_summary}; "
                        f"core={core_symbol} target={target_core_pct*100:.0f}%"
                    )
                    await broadcast({
                        "type": "policy_rebalance_complete",
                        "user": user.username, "sells": sells_done,
                    })
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[Rebalance] loop error: {e}")
        await asyncio.sleep(60)


async def background_dca_core_etf():
    """
    Task 13 — Monthly Dollar-Cost-Averaging into the long-term core ETF (SPY).

    On the configured day-of-month (default: 1st trading day of the month),
    spends `dca_pct_of_cash` (default 50%) of available cash on the configured
    `core_etf_symbol` (default SPY). Skips automatically if:
      - DCA disabled
      - Already DCA'd this month
      - Market closed
      - Available cash below $10 (avoids dust trades)

    The point is to gradually rebuild the 50/45/5 core/satellite/cash mix
    without forcing a panic-sell of profitable satellite positions.
    """
    await asyncio.sleep(300)  # let server settle before first DCA check
    while True:
        try:
            now = datetime.utcnow()
            db = next(get_db())
            try:
                users = db.query(User).all()
                for user in users:
                    dca_enabled = get_setting(db, "dca_enabled", user.id, "false") == "true"
                    if not dca_enabled:
                        continue

                    target_day = int(get_setting(db, "dca_day_of_month", user.id, "1"))
                    pct_of_cash = float(get_setting(db, "dca_pct_of_cash", user.id, "50.0")) / 100
                    core_symbol = get_setting(db, "core_etf_symbol", user.id, "SPY")
                    last_run_str = get_setting(db, "dca_last_run", user.id, "")

                    # Already ran in current month?
                    if last_run_str:
                        try:
                            last_run = datetime.fromisoformat(last_run_str)
                            if last_run.year == now.year and last_run.month == now.month:
                                continue
                        except Exception:
                            pass

                    # Run on or after target day-of-month, weekday only
                    if now.day < target_day or now.weekday() >= 5:
                        continue
                    # US market hours only (14:30 — 21:00 UTC ≈ 9:30am — 4pm ET)
                    if not (14 <= now.hour < 21):
                        continue

                    engine = TradingEngine(db, user.id)
                    if not engine.alpaca:
                        continue
                    try:
                        acct = engine.alpaca.get_account()
                        cash = float(acct.cash)
                    except Exception as e:
                        logger.error(f"[DCA] {user.username} cash fetch error: {e}")
                        continue

                    spend = round(cash * pct_of_cash, 2)
                    if spend < 10.0:
                        logger.info(
                            f"[DCA] {user.username} skipped — only ${spend:.2f} available "
                            f"({pct_of_cash*100:.0f}% of ${cash:.2f}), below $10 minimum"
                        )
                        set_setting(db, "dca_last_run", now.isoformat(), user.id)
                        continue

                    quote = md.get_stock_quote(core_symbol)
                    if not quote or not quote.get("current"):
                        logger.warning(f"[DCA] {core_symbol} quote unavailable, skipping today")
                        continue
                    price = float(quote["current"])
                    qty = round(spend / price, 4)
                    if qty < 0.001:
                        continue

                    logger.info(
                        f"[DCA] {user.username} buying {qty} {core_symbol} @ ${price:.2f} "
                        f"(${spend:.2f} = {pct_of_cash*100:.0f}% of ${cash:.2f} cash)"
                    )
                    result = engine.execute_buy(
                        core_symbol, qty, price,
                        ai_triggered=True,
                        confidence=1.0,
                        reasoning=f"[DCA] Monthly core-ETF buy ({pct_of_cash*100:.0f}% of available cash)",
                    )
                    if result.get("success"):
                        set_setting(db, "dca_last_run", now.isoformat(), user.id)
                        await broadcast({
                            "type": "dca_executed",
                            "symbol": core_symbol,
                            "qty": qty, "price": price, "spent": spend,
                        })
                        logger.info(f"[DCA] {core_symbol} buy filled: {result}")
                    else:
                        logger.error(f"[DCA] {core_symbol} buy failed: {result}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[DCA] Loop error: {e}")
        await asyncio.sleep(3600)  # check hourly; only acts once per month per user


async def background_rl_pipeline():
    """
    Task 18 — End-to-end RL MLOps pipeline (daily).

    Replaces the old single-step "train + overwrite" loop with a full cycle:
      1. back-fill rewards on JSONL
      2. refresh attribution report
      3. train candidate XGBoost on data older than 7 days
      4. score candidate AND production model on the last 7 days (holdout)
      5. decide: promote / shadow / reject based on directional accuracy
      6. update rl_policy_model.pkl (production) or rl_policy_shadow.pkl
      7. trigger LoRA retraining if +5000 new labeled records accumulated

    Pipeline state is persisted to rl_models/registry.json — readable via the
    /api/rl/pipeline endpoint.
    """
    await asyncio.sleep(300)   # wait 5 min after startup
    while True:
        try:
            import rl_pipeline as _pipe
            # 14-day holdout (instead of default 7) — temporarily widened
            # because the 5/15→5/20 broken-AI period contaminated last week.
            # Revert to 7 once 1 week of clean signals accumulates.
            report = await asyncio.get_event_loop().run_in_executor(None, lambda: _pipe.run_cycle(holdout_days=14))
            status = report.get("status", "unknown")
            decision = (report.get("decision") or {}).get("decision", "-")
            logger.info(f"[RL Pipeline] cycle complete: status={status}  decision={decision}")
        except Exception as e:
            logger.error(f"[RL Pipeline] Error: {e}", exc_info=True)
        await asyncio.sleep(21600)   # one cycle every 6 hours (4× per day)


async def background_llm_shootout_loop():
    """
    Task 19 — Daily LLM backend shootout + auto-promotion.

    Replaces the static `ollama_model` setting with an evidence-driven choice.
    Every 24h:
      1. Pull rolling 7d holdout from rl_training_data.jsonl
      2. Run each candidate model via the SAME production prompt path
      3. Compare directional_accuracy + mean_realised_reward
      4. If winner beats current `ollama_model` by ≥5pp dir_acc, auto-update
         the `ollama_model` and `ollama_host` DB settings
      5. Persist full report to rl_models/llm_shootout/*.json

    Born from 2026-05-21 incident: production silently used qwen3.5:35b
    pointing at a dead daemon for 5 days, no auto-detection. Now host
    reachability is verified each cycle and bad config self-corrects.
    """
    await asyncio.sleep(900)   # wait 15 min after startup for warm cache
    while True:
        try:
            import rl_llm_shootout as _shoot
            from database import SessionLocal
            db = SessionLocal()
            try:
                report = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _shoot.run_shootout(db_session=db, auto_promote=True)
                )
            finally:
                db.close()
            winner = report.get("winner")
            promoted = report.get("promoted", False)
            logger.info(f"[LLM Shootout] winner={winner} promoted={promoted}")
            try:
                await broadcast({"type":"llm_shootout","report":report})
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[LLM Shootout] Error: {e}", exc_info=True)
        await asyncio.sleep(86400)   # one cycle every 24h


async def background_dynamic_watchlist_loop():
    """
    Task 21 — Dynamic watchlist discovery (every 6h).

    Replaces hand-curated watchlist with market-driven discovery:
      • US top movers (yfinance day_gainers / most_actives)
      • News mention frequency
      • Social sentiment surge
      • Sector peer expansion
      • Stale-prune (no signals in 14d, not held, not always-keep)

    Created 2026-05-24 after user feedback "watchlist 不应该写死". The earlier
    keyword-maintenance anti-pattern (CATALYST_MAP) had a sibling: hand-curated
    symbol list. Both are now data-driven.
    """
    await asyncio.sleep(300)   # 5 min after startup
    while True:
        try:
            import dynamic_watchlist as _dw
            from database import SessionLocal
            db = SessionLocal()
            try:
                report = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _dw.run_discovery_cycle(db_session=db)
                )
            finally:
                db.close()
            logger.info(
                f"[DynamicWL] cycle complete: "
                f"{report.get('before_count')} → {report.get('after_count')} symbols  "
                f"(+{report.get('added_count')} -{report.get('pruned_count')})"
            )
        except Exception as e:
            logger.error(f"[DynamicWL] Error: {e}", exc_info=True)
        await asyncio.sleep(6 * 3600)   # every 6h


async def background_llm_catalyst_loop():
    """
    Task 20 — LLM-driven catalyst extraction (every 30 min).

    Replaces static CATALYST_MAP keyword-matching for novel events. For each
    watchlist symbol, pulls fresh news + classifies headlines via LLM, caches
    results. Output is consumed by detect_catalysts_for_symbol() merge path
    (see news_intelligence integration) and reaches the AI prompt via the
    existing build_catalyst_context flow.

    Created 2026-05-23 after SerenityAlphaTrader missed the US-gov-takes-Intel-stake
    catalyst (no keyword for "government stake" in static map).
    """
    await asyncio.sleep(120)   # wait 2 min after startup
    while True:
        try:
            import llm_catalyst_extractor as _lce
            import json as _json
            from database import SessionLocal
            db = SessionLocal()
            try:
                wl_row = db.query(Settings).filter(Settings.key=="watchlist").first()
                watchlist = _json.loads(wl_row.value or "[]") if wl_row else []
            finally:
                db.close()

            # ── SECTOR-WIDE catalyst capture first (user 2026-05-27: capture
            # all GPU/chip market dynamics). Maps broad semi/AI news → focus
            # stocks before the per-ticker pass. ──
            try:
                import dynamic_watchlist as _dw2
                focus_setting = ""
                _db2 = SessionLocal()
                try:
                    _fs = _db2.query(Settings).filter(Settings.key=="focus_themes").first()
                    focus_setting = _fs.value if _fs else "core_semiconductors,gpu_downstream_supply,physical_ai_robotics"
                finally:
                    _db2.close()
                focus_syms = []
                for t in focus_setting.split(","):
                    focus_syms.extend(_dw2.THEMATIC_UNIVERSES.get(t.strip(), []))
                focus_syms = list(dict.fromkeys(focus_syms))
                if focus_syms:
                    sector_cats = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _lce.extract_sector_catalysts(focus_syms, hours_back=12)
                    )
                    for sym, cats in (sector_cats or {}).items():
                        for c in cats:
                            logger.info(
                                f"[LLM SectorCatalyst] {sym} {c.get('catalyst_level')} "
                                f"{c.get('llm_direction','?')} ({c.get('llm_confidence',0):.2f}): "
                                f"{c.get('news_title','')[:90]}"
                            )
            except Exception as _se:
                logger.warning(f"[LLM SectorCatalyst] failed: {_se}")

            total_catalysts = 0
            for sym in watchlist:
                try:
                    cats = await asyncio.get_event_loop().run_in_executor(
                        None, lambda s=sym: _lce.extract_catalysts_for_symbol(s, hours_back=24)
                    )
                    if cats:
                        total_catalysts += len(cats)
                        # Log only STRONG / MEDIUM catalysts for noise reduction
                        for c in cats:
                            if c.get("catalyst_level") in ("STRONG", "MEDIUM"):
                                logger.info(
                                    f"[LLM Catalyst] {sym} {c['catalyst_level']} "
                                    f"{c.get('llm_direction','?')} ({c.get('llm_confidence',0):.2f}): "
                                    f"{c.get('news_title','')[:100]}"
                                )
                except Exception as e:
                    logger.debug(f"[LLM Catalyst] {sym}: {e}")

            stats = _lce.cache_stats()
            logger.info(f"[LLM Catalyst] cycle done: {total_catalysts} catalysts across {len(watchlist)} symbols; "
                        f"cache: {stats['total_classifications']} total / {stats['catalyst_count']} catalysts")
        except Exception as e:
            logger.error(f"[LLM Catalyst] Error: {e}", exc_info=True)

        await asyncio.sleep(900)   # every 15 min (sharper chip-news reaction, user 5/27)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8888, reload=True)
