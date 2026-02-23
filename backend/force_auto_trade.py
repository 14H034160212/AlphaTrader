import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, set_setting
db = next(get_db())
set_setting(db, "auto_trade_min_confidence", "0.5")  # Lower threshold for training
set_setting(db, "risk_per_trade_pct", "5.0")         # Increase risk for more visible trades
db.commit()
print("Training parameters adjusted: min confidence=0.5, risk=5.0%")
