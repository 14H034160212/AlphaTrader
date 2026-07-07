#!/usr/bin/env bash
# Pull current holdings from Alpaca + IBKR + Moomoo and dump as markdown.
# Output: ~/serenity-trader-stack/reports/snapshots/portfolio-YYYY-MM-DD.md
#
# Requires AlphaTrader's backend code at /data/qbao775/AlphaTrader/backend/.
# IBKR + Moomoo need their daemons running; if not, they're skipped gracefully.

set -e
OUT="/home/qbao775/serenity-trader-stack/reports/snapshots/portfolio-$(date +%Y-%m-%d).md"
mkdir -p "$(dirname "$OUT")"

cd "/data/qbao775/AlphaTrader/backend" 2>/dev/null || { echo "AlphaTrader backend missing"; exit 1; }

conda run -n alphatrader python3 -u - <<'PYEOF' > "$OUT"
import sys, os, datetime
sys.path.insert(0, '.')
from database import SessionLocal, get_setting

print(f"# Portfolio snapshot — {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC}\n")

# --- Alpaca ---
try:
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db,'alpaca_api_key',1); s = get_setting(db,'alpaca_secret_key',1)
    u = get_setting(db,'alpaca_base_url',1,'https://api.alpaca.markets'); db.close()
    api = tradeapi.REST(k,s,u)
    a = api.get_account()
    print("## Alpaca US")
    print(f"- equity: ${float(a.equity):.2f} | cash: ${float(a.cash):.2f}")
    print("- positions:")
    for p in api.list_positions():
        if float(p.market_value) < 1: continue
        print(f"  - {p.symbol}: {float(p.qty):.4f}sh ${float(p.market_value):.2f} pl ${float(p.unrealized_pl):+.2f}")
except Exception as e:
    print(f"## Alpaca\n_(failed: {e})_")

# --- IBKR ---
try:
    from ib_insync import IB
    ib = IB(); ib.connect('127.0.0.1', 4003, clientId=200, timeout=8)
    nl = next((float(r.value) for r in ib.accountSummary() if r.tag == 'NetLiquidation'), 0)
    cash = next((float(r.value) for r in ib.accountSummary() if r.tag == 'TotalCashValue'), 0)
    print(f"\n## IBKR US\n- NetLiq: ${nl:.2f} | cash: ${cash:.2f}")
    pos = ib.positions()
    if pos:
        print("- positions:")
        for p in pos:
            print(f"  - {p.contract.symbol}: {p.position}sh @ ${p.avgCost:.2f}")
    else:
        print("- positions: (none)")
    ib.disconnect()
except Exception as e:
    print(f"\n## IBKR\n_(gateway offline or failed: {type(e).__name__})_")

# --- Moomoo HK ---
try:
    import futu as ft
    ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK,
        host='127.0.0.1', port=11111, security_firm=ft.SecurityFirm.FUTUAU)
    ret, pos = ctx.position_list_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True)
    ret2, info = ctx.accinfo_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True, currency=ft.Currency.HKD)
    print(f"\n## Moomoo HK")
    if ret2 == ft.RET_OK:
        r = info.iloc[0]
        print(f"- cash: HK${r['cash']:.0f} | power: HK${r['power']:.0f}")
    if ret == ft.RET_OK and not pos.empty:
        print("- positions:")
        for _, p in pos.iterrows():
            print(f"  - {p['code']}: {p['qty']}sh cost HK${p['cost_price']:.2f} → HK${p['nominal_price']:.2f} | pl HK${p['pl_val']:+.0f} ({p['pl_ratio']:+.1f}%)")
    ctx.close()
except Exception as e:
    print(f"\n## Moomoo HK\n_(OpenD offline or failed: {type(e).__name__})_")
PYEOF
echo "✓ $OUT"
