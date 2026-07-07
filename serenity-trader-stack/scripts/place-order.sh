#!/usr/bin/env bash
# Usage: place-order.sh BUY SPY 1.5 limit 760
#        place-order.sh SELL MU 10 market
# Wraps AlphaTrader's existing broker code. Auto-routes US → Alpaca, HK → Moomoo.
set -e
SIDE="${1:?BUY|SELL}"
SYM="${2:?ticker}"
QTY="${3:?quantity}"
TYPE="${4:-market}"
LIMIT="${5:-}"

cd "/data/qbao775/AlphaTrader/backend" 2>/dev/null || exit 1
conda run -n alphatrader python3 -u - <<PYEOF
import sys; sys.path.insert(0,'.')
from database import SessionLocal, get_setting
sym = '$SYM'; side = '$SIDE'; qty = float('$QTY'); otype = '$TYPE'
limit = '$LIMIT'

if '.HK' in sym or '.CN' in sym:
    print(f'Routing {sym} → Moomoo (HK/CN)')
    print('Manual via App for now (OpenAPI permission still blocked).')
    print(f'In App: search {sym}, side={side}, qty={qty}, type={otype}, limit={limit}')
else:
    print(f'Routing {sym} → Alpaca (US)')
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
    u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
    api = tradeapi.REST(k,s,u)
    kwargs = dict(symbol=sym, qty=qty, side=side.lower(), type=otype, time_in_force='day')
    if otype == 'limit' and limit:
        kwargs['limit_price'] = float(limit)
        kwargs['extended_hours'] = True
    o = api.submit_order(**kwargs)
    print(f'✓ Submitted: {o.symbol} {o.side} {o.qty}sh {o.type} status={o.status} (id={o.id[:8]})')
PYEOF
