import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db
from trading_engine import TradingEngine

db = next(get_db())
engine = TradingEngine(db)
summ = engine.get_portfolio_summary()

print(f"Trading Provider: {summ['provider']}")
print(f"Live Cash Balance: ${summ['cash']}")
print(f"Total Equity: ${summ['total_equity']}")

if float(summ['cash']) > 0:
    print("✅ Live Connection Verified! AlphaTrader is armed and dangerous.")
else:
    print("⚠️ Connection returned 0 balance. Please check if keys are correct and account is funded.")
