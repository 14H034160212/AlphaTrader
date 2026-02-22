import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, set_setting
db = next(get_db())
set_setting(db, "auto_trade_enabled", "true")
db.commit()
print("Auto-trading enabled!")
