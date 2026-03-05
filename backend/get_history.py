import sys, json, datetime
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, Trade, Position
from trading_engine import TradingEngine

db = next(get_db())
engine = TradingEngine(db, user_id=1)

if engine.use_alpaca:
    try:
        history = engine.alpaca.get_portfolio_history(period="1W", timeframe="1D")
        out = []
        for i in range(len(history.timestamp)):
            dt = datetime.datetime.fromtimestamp(history.timestamp[i]).strftime('%Y-%m-%d')
            eq = history.equity[i]
            pl = history.profit_loss[i]
            pl_pct = history.profit_loss_pct[i]
            out.append({"date": dt, "equity": eq, "daily_pnl": pl, "daily_pnl_pct": pl_pct})
        print("--- PORTFOLIO HISTORY ---")
        print(json.dumps(out, indent=2))
    except Exception as e:
        print(f"Error fetching history: {e}")

# Get signals and pending trades
print("\n--- AI SIGNALS ---")
try:
    result = db.execute("SELECT symbol, signal, confidence, reasoning FROM ai_signals ORDER BY timestamp DESC LIMIT 5").fetchall()
    for row in result:
        print(row)
except Exception as e:
    print(e)

print("\n--- PENDING TRADES ---")
try:
    result = db.execute("SELECT symbol, side, execute_on, status FROM pending_trades WHERE execute_on >= date('now')").fetchall()
    for row in result:
        print(row)
except Exception as e:
    print(e)
