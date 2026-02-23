import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, set_setting
import logging

logging.basicConfig(level=logging.INFO)
db = next(get_db())

# Update settings for LIVE trading
set_setting(db, "alpaca_api_key", "AKFXJMHJMY5FGIQ6BOHFMNV277")
set_setting(db, "alpaca_secret_key", "EYMG9LGMZk7Ch15y4qepy2smzodsQZzpFeNx2aFeza5J")
set_setting(db, "alpaca_paper_mode", "false")
set_setting(db, "auto_trade_enabled", "true")  # Turn on auto-trade for real battle

db.commit()
print("🚀 Live Trading Activated! Alpaca credentials stored and Paper Mode disabled.")
