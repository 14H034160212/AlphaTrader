"""
FastAPI main application - REST API + WebSocket server for global stock trading platform.
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
from trading_engine import TradingEngine
from database import create_tables, get_db, get_setting, set_setting, Trade, AISignal, WatchedStock, Settings

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
    # Initialize default settings
    db = next(get_db())
    defaults = {
        "cash_balance": "100000.0",
        "initial_cash": "100000.0",
        "auto_trade_enabled": "false",
        "auto_trade_min_confidence": "0.75",
        "risk_per_trade_pct": "2.0",
        "risk_per_trade_pct": "2.0",
        "deepseek_api_key": "",
        "ai_provider": "deepseek_api",  # 'deepseek_api' or 'ollama'
        "watchlist": json.dumps(md.DEFAULT_WATCHLIST),
        "refresh_interval_seconds": "30",
    }
    for key, value in defaults.items():
        existing = db.query(Settings).filter(Settings.key == key).first()
        if not existing:
            from database import set_setting as _set
            _set(db, key, value)

    yield
    logger.info("Shutting down trading platform")


app = FastAPI(
    title="Global Stock Trading Platform",
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
    
# ─────────────────────────────────────────────
# Background price refresh
# ─────────────────────────────────────────────

async def background_price_refresh():
    """Continuously refresh prices and broadcast to WebSocket clients."""
    global price_cache, market_cache, last_market_fetch
    while True:
        try:
            db = next(get_db())
            interval = int(get_setting(db, "refresh_interval_seconds", "30"))
            watchlist_json = get_setting(db, "watchlist", json.dumps(md.DEFAULT_WATCHLIST))
            watchlist = json.loads(watchlist_json)

            # Also include positions' symbols
            engine = TradingEngine(db)
            positions = engine.get_all_positions()
            pos_symbols = [p.symbol for p in positions]
            all_symbols = list(set(watchlist + pos_symbols))

            # Fetch prices
            new_prices = {}
            for symbol in all_symbols[:20]:  # Limit to avoid rate limiting
                try:
                    quote = md.get_stock_quote(symbol)
                    if quote:
                        new_prices[symbol] = quote["current"]
                        price_cache[symbol] = quote
                except Exception as e:
                    logger.error(f"Error fetching {symbol}: {e}")

            # Update position prices
            if new_prices:
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
    """Continuously analyze watchlist and trigger auto-trades if enabled."""
    while True:
        try:
            db = next(get_db())
            auto_trade_enabled = get_setting(db, "auto_trade_enabled", "false") == "true"
            if not auto_trade_enabled:
                await asyncio.sleep(60) # check again in a minute
                continue

            # --- EXPERIMENTAL: Auto-Switch to Live Trading upon Funding ---
            try:
                # If currently in paper mode, but live keys are provided, check real balance
                alpaca_paper = get_setting(db, "alpaca_paper_mode", "true") == "true"
                if alpaca_paper:
                    live_key = get_setting(db, "alpaca_api_key", "")
                    live_secret = get_setting(db, "alpaca_secret_key", "")
                    # Note: To work, user must put LIVE keys in settings, but leave paper_trading = ON until funded.
                    if live_key and live_key.startswith("AK") and live_secret:
                        import alpaca_trade_api as tradeapi
                        temp_live_alpaca = tradeapi.REST(live_key, live_secret, "https://api.alpaca.markets", api_version='v2')
                        live_account = temp_live_alpaca.get_account()
                        if float(live_account.cash) > 10.0:  # If more than $10 arrived
                            logger.info("💰 LIVE FUNDS DETECTED! Auto-switching to Live Trading!")
                            from database import set_setting
                            set_setting(db, "alpaca_paper_mode", "false")
                            db.commit()
                            
                            # Send OpenClaw notification
                            webhook_url = get_setting(db, "openclaw_webhook_url", "")
                            if webhook_url:
                                import requests
                                msg = f"🚨 **FUNDS ARRIVED!** System auto-switched to Live Trading. Real cash: ${float(live_account.cash):.2f}"
                                requests.post(webhook_url, json={"event": "funding_arrived", "message": msg}, timeout=5)
            except Exception as e:
                logger.warning(f"Auto-switch live funding check skipped/failed: {e}")
            # --------------------------------------------------------------

            logger.info("Starting background auto-trade analysis cycle...")
            api_key = get_setting(db, "deepseek_api_key", "")
            ai_provider = get_setting(db, "ai_provider", "deepseek_api")
            watchlist_json = get_setting(db, "watchlist", json.dumps(md.DEFAULT_WATCHLIST))
            watchlist = json.loads(watchlist_json)
            
            engine = TradingEngine(db)
            summary = engine.get_portfolio_summary()
            portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"

            for symbol in watchlist:
                try:
                    # Delay between analysis to avoid rate limits
                    await asyncio.sleep(5) 
                    
                    quote = md.get_stock_quote(symbol)
                    if not quote: continue
                    history = md.get_stock_history(symbol, period="6mo")
                    indicators = md.get_technical_indicators(symbol)
                    news = md.get_stock_news(symbol)
                    
                    signal = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context)
                    
                    # Store signal
                    db_signal = AISignal(
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

                    # Execute trade
                    if signal.get("signal") in ("BUY", "SELL"):
                        auto_result = engine.auto_trade(signal, quote["current"])
                        if auto_result.get("success"):
                            logger.info(f"Auto-trade executed for {symbol}: {auto_result}")
                            await broadcast({
                                "type": "auto_trade", 
                                "signal": signal, 
                                "trade": auto_result,
                                "timestamp": datetime.utcnow().isoformat()
                            })
                            # Webhook notification for OpenClaw would go here!
                            openclaw_webhook_url = get_setting(db, "openclaw_webhook_url", "")
                            if openclaw_webhook_url:
                                import requests
                                try:
                                    requests.post(openclaw_webhook_url, json={"event": "auto_trade", "data": auto_result}, timeout=5)
                                    logger.info("Sent OpenClaw webhook notification.")
                                except Exception as err:
                                    logger.error(f"OpenClaw webhook failed: {err}")
                except Exception as inner_e:
                    logger.error(f"Error auto-trading {symbol}: {inner_e}")

        except Exception as e:
            logger.error(f"Background auto-trade loop error: {e}")

        # Sleep for an hour before the next full cycle analysis
        await asyncio.sleep(3600)


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
        active_connections.remove(ws)


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


@app.get("/api/stock/{symbol}")
async def get_stock(symbol: str):
    """Get full data for a single stock."""
    symbol = symbol.upper()
    quote = md.get_stock_quote(symbol)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")
    history = md.get_stock_history(symbol)
    indicators = md.get_technical_indicators(symbol)
    news = md.get_stock_news(symbol)
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
async def get_portfolio(db: Session = Depends(get_db)):
    """Get portfolio summary and positions."""
    engine = TradingEngine(db)
    return engine.get_portfolio_summary()


@app.get("/api/trades")
async def get_trades(limit: int = 50, db: Session = Depends(get_db)):
    """Get trade history."""
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(limit).all()
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
async def execute_trade(request: TradeRequest, db: Session = Depends(get_db)):
    """Execute a manual trade."""
    engine = TradingEngine(db)
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

    await broadcast({"type": "trade_executed", "trade": result.get("trade")})
    return result


@app.post("/api/analyze")
async def analyze_stock(request: AnalyzeRequest, db: Session = Depends(get_db)):
    """Run DeepSeek-R1 analysis on a stock."""
    symbol = request.symbol.upper()
    api_key = get_setting(db, "deepseek_api_key", "")
    ai_provider = get_setting(db, "ai_provider", "deepseek_api")

    quote = md.get_stock_quote(symbol)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    history = md.get_stock_history(symbol, period="6mo")
    indicators = md.get_technical_indicators(symbol)
    news = md.get_stock_news(symbol)

    # Portfolio context
    engine = TradingEngine(db)
    summary = engine.get_portfolio_summary()
    portfolio_context = f"Portfolio equity: ${summary['total_equity']:,.2f}, Cash: ${summary['cash']:,.2f}"

    signal = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news, portfolio_context)

    # Store signal in DB
    db_signal = AISignal(
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
    if signal.get("signal") in ("BUY", "SELL"):
        auto_result = engine.auto_trade(signal, quote["current"])
        if auto_result.get("success"):
            await broadcast({"type": "auto_trade", "signal": signal, "trade": auto_result})

    return {"signal": signal, "quote": quote, "auto_trade": auto_result}


@app.post("/api/analyze-portfolio")
async def analyze_portfolio(db: Session = Depends(get_db)):
    """Run DeepSeek-R1 portfolio analysis."""
    api_key = get_setting(db, "deepseek_api_key", "")
    ai_provider = get_setting(db, "ai_provider", "deepseek_api")
    engine = TradingEngine(db)
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
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """Chat with DeepSeek-R1 about markets."""
    api_key = get_setting(db, "deepseek_api_key", "")
    ai_provider = get_setting(db, "ai_provider", "deepseek_api")
    engine = TradingEngine(db)
    summary = engine.get_portfolio_summary()
    context = f"Portfolio equity: ${summary['total_equity']:,.2f}"
    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    response = ai.chat_with_ai(ai_provider, api_key, messages, context)
    return {"response": response}


@app.get("/api/signals")
async def get_signals(limit: int = 20, db: Session = Depends(get_db)):
    """Get recent AI signals."""
    signals = db.query(AISignal).order_by(AISignal.timestamp.desc()).limit(limit).all()
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
async def get_watchlist(db: Session = Depends(get_db)):
    """Get current watchlist."""
    watchlist_json = get_setting(db, "watchlist", json.dumps(md.DEFAULT_WATCHLIST))
    return {"symbols": json.loads(watchlist_json)}


@app.post("/api/watchlist")
async def update_watchlist(item: WatchlistUpdate, db: Session = Depends(get_db)):
    """Add or remove from watchlist."""
    symbol = item.symbol.upper()
    watchlist_json = get_setting(db, "watchlist", json.dumps(md.DEFAULT_WATCHLIST))
    watchlist = set(json.loads(watchlist_json))
    if item.action == "add":
        watchlist.add(symbol)
    elif item.action == "remove":
        watchlist.discard(symbol)
    
    set_setting(db, "watchlist", json.dumps(list(watchlist)))
    return {"watchlist": list(watchlist)}

# ─────────────────────────────────────────────
# OpenClaw Integration
# ─────────────────────────────────────────────

@app.post("/api/openclaw/webhook")
async def openclaw_webhook(request: OpenClawWebhook, db: Session = Depends(get_db)):
    """Endpoint for OpenClaw Skill to query portfolio or analyze stocks remotely."""
    command = request.command.lower().strip()
    
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
async def get_settings(db: Session = Depends(get_db)):
    """Get all settings (API key is masked)."""
    keys = [
        "auto_trade_enabled", "auto_trade_min_confidence",
        "risk_per_trade_pct", "refresh_interval_seconds", "ai_provider",
        "alpaca_paper_mode"
    ]
    result = {}
    for key in keys:
        result[key] = get_setting(db, key, "")
    
    # Mask deepseek api key
    api_key = get_setting(db, "deepseek_api_key", "")
    result["deepseek_api_key_set"] = bool(api_key)
    result["deepseek_api_key_preview"] = f"{api_key[:8]}..." if len(api_key) > 8 else ("" if not api_key else api_key)

    # Mask alpaca keys
    alpaca_key = get_setting(db, "alpaca_api_key", "")
    alpaca_secret = get_setting(db, "alpaca_secret_key", "")
    result["alpaca_api_key_set"] = bool(alpaca_key)
    result["alpaca_secret_key_set"] = bool(alpaca_secret)
    result["alpaca_api_key_preview"] = f"{alpaca_key[:8]}..." if len(alpaca_key) > 8 else ("" if not alpaca_key else alpaca_key)
    
    return result


@app.post("/api/settings")
async def update_setting(update: SettingsUpdate, db: Session = Depends(get_db)):
    """Update a setting."""
    set_setting(db, update.key, update.value)
    return {"key": update.key, "updated": True}


@app.post("/api/reset-portfolio")
async def reset_portfolio(db: Session = Depends(get_db)):
    """Reset paper trading portfolio to initial state."""
    from database import Position
    db.query(Trade).delete()
    db.query(Position).delete()
    db.query(AISignal).delete()
    db.commit()
    set_setting(db, "cash_balance", "100000.0")
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
