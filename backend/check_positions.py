import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, Position
db = next(get_db())
for p in db.query(Position).all():
    print(f"{p.symbol}: {p.quantity} @ {p.avg_cost}")
