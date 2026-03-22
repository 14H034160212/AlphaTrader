import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db

db = next(get_db())

print("--- LATEST SIGNAL PER SYMBOL ---")
query = """
SELECT symbol, signal, confidence, timestamp, reasoning 
FROM ai_signals a
WHERE timestamp = (
    SELECT MAX(timestamp) FROM ai_signals b WHERE b.symbol = a.symbol
)
ORDER BY timestamp DESC;
"""
try:
    results = db.execute(query).fetchall()
    latest_signals = {row[0]: {"signal": row[1], "confidence": row[2], "time": row[3], "reason": row[4]} for row in results}
    
    current_holdings = ['XOM', 'GLD', 'LMT', 'NOC', 'RTX', 'SLV', 'IAU']
    
    print("\n[Current Holdings Analysis]")
    for sym in current_holdings:
        if sym in latest_signals:
            sig = latest_signals[sym]
            print(f"{sym}: {sig['signal']} (Conf: {sig['confidence']}) - {sig['time']}")
        else:
            print(f"{sym}: NO SIGNAL")
            
    print("\n[Strong BUY Recommendations (Conf >= 0.80)]")
    for sym, sig in latest_signals.items():
        if sig['signal'] == 'BUY' and float(sig['confidence']) >= 0.80 and sym not in current_holdings:
            print(f"{sym}: {sig['signal']} (Conf: {sig['confidence']}) - {sig['time']}")
            
    print("\n[Strong SELL/HOLD for non-holdings (FYI)]")
    for sym, sig in latest_signals.items():
        if sig['signal'] == 'SELL' and float(sig['confidence']) >= 0.80:
            print(f"{sym}: {sig['signal']} (Conf: {sig['confidence']}) - {sig['time']}")

except Exception as e:
    print(f"Error: {e}")
