import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, get_setting, set_setting

db = next(get_db())
print(f"Auto Trade: {get_setting(db, 'auto_trade_enabled')}")
print(f"AI Provider: {get_setting(db, 'ai_provider')}")
print(f"Refresh Int: {get_setting(db, 'refresh_interval_seconds')}")
