import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, get_setting, set_setting

db = next(get_db())

alpaca_key    = get_setting(db, "alpaca_api_key", "")
alpaca_secret = get_setting(db, "alpaca_secret_key", "")
paper_mode    = get_setting(db, "alpaca_paper_mode", "true")

print(f"API Key set:    {'YES (' + alpaca_key[:6] + '...)' if alpaca_key else 'NO'}")
print(f"Secret set:     {'YES' if alpaca_secret else 'NO'}")
print(f"Paper Mode now: {paper_mode}")
