#!/usr/bin/env bash
# Show actual vs target allocation across passive-index / value / chokepoint buckets.
# Targets are read from this file (edit them to change strategy).

TARGET_INDEX_PCT=70
TARGET_VALUE_PCT=20
TARGET_CHOKEPOINT_PCT=8
TARGET_CASH_PCT=2

# These define what counts as "index" vs "value" vs "chokepoint"
INDEX_TICKERS="SPY VOO VTI SPLG QQQ 2800.HK 02800.HK 3033.HK 03033.HK"

cd "/data/qbao775/AlphaTrader/backend" 2>/dev/null || exit 1
conda run -n alphatrader python3 - <<'PYEOF'
import sys, os
sys.path.insert(0,'.')
from database import SessionLocal, get_setting
import alpaca_trade_api as tradeapi

INDEX = set("SPY VOO VTI SPLG QQQ 2800.HK 02800.HK 3033.HK 03033.HK".split())

db = SessionLocal()
k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
api = tradeapi.REST(k,s,u)

eq = float(api.get_account().equity)
cash = float(api.get_account().cash)
buckets = {'index':0, 'other':0, 'cash':cash}
for p in api.list_positions():
    if float(p.market_value) < 1: continue
    mv = float(p.market_value)
    if p.symbol in INDEX: buckets['index'] += mv
    else: buckets['other'] += mv

print(f"=== Alpaca alocation (equity ${eq:.2f}) ===")
for k_, v in buckets.items():
    pct = v / eq * 100 if eq else 0
    print(f"  {k_:12} ${v:8.2f}  ({pct:5.1f}%)")
print()
print(f"Target: index 70% / value 20% / chokepoint 8% / cash 2%")
idx_pct = buckets['index']/eq*100
delta = idx_pct - 70
print(f"Index actual: {idx_pct:.1f}%  → vs target 70%: {delta:+.1f}pp")
if abs(delta) > 5:
    print(f"⚠️  > 5pp off target — consider rebalancing")
PYEOF
