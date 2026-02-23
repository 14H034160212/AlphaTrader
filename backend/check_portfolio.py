import sys, json
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db
from trading_engine import TradingEngine

db = next(get_db())
engine = TradingEngine(db)
summ = engine.get_portfolio_summary()
print(json.dumps(summ, indent=2))
