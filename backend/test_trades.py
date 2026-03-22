import sys
sys.path.append('/home/qbao775/.local/lib/python3.8/site-packages')
sys.path.append('/data/qbao775/AlphaTrader/backend')
from database import get_db, Trade
from trading_engine import TradingEngine
from datetime import datetime, date

def check_pnl_and_trades():
    db = next(get_db())
    # User 1 is default
    engine = TradingEngine(db, 1)
    try:
        acct = engine.alpaca.get_account()
        print("--- ALPACA ACCOUNT ---")
        print(f"Equity: {acct.equity}")
        print(f"Cash: {acct.cash}")
        # Could be missing or strings
        for k in ['equity', 'cash', 'unrealized_pl', 'unrealized_intraday_pl', 'portfolio_value']:
            print(f"{k}: {getattr(acct, k, 'N/A')} ({type(getattr(acct, k, None))})")
    except Exception as e:
        print(f"Alpaca Error: {e}")
        
    print("\n--- TRADES FROM YESTERDAY (3/9) ---")
    yesterday_str = '2026-03-09'
    trades = db.query(Trade).filter(Trade.timestamp >= '2026-03-09 00:00:00', Trade.timestamp < '2026-03-10 00:00:00').all()
    for t in trades:
        print(f"{t.timestamp}: {t.side} {t.quantity} {t.symbol} @ {t.price}")

if __name__ == '__main__':
    check_pnl_and_trades()
