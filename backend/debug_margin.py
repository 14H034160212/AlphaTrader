import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db
from trading_engine import TradingEngine

db = next(get_db())
engine = TradingEngine(db)

cash = engine.get_cash_balance()
total_equity = cash
for p in engine.get_all_positions():
    total_equity += p.quantity * p.current_price

print(f"Cash: {cash}")
print(f"Total Equity: {total_equity}")
print(f"Cash + Total Equity: {cash + total_equity}")
