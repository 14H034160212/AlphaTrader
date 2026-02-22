import sys
import json
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, set_setting
from market_data import DEFAULT_WATCHLIST

db = next(get_db())
set_setting(db, "watchlist", json.dumps(DEFAULT_WATCHLIST))
db.commit()
print(f"Watchlist updated to {len(DEFAULT_WATCHLIST)} diverse assets.")
