#!/usr/bin/env python3
"""
Auto-deploy NZ$100k to Plan D allocation when it arrives at Alpaca.

Plan D (Future-Aware Balanced, ratified 2026-06-30 by user):
  70% SPY   — broad US market core
  15% QQQ   — tech participation without single-name risk
  12% BRK.B — Buffett quality + defensive cash buffer
   3% cash  — dry powder for corrections

Sequence when triggered:
  1. Sell existing NVDA + MU (small positions, don't fit Plan D concentration risk)
  2. Wait for fills (~10s)
  3. Recompute total account value
  4. Place 3 buy orders: SPY (70%), QQQ (15%), BRK.B (12%), leave 3% cash

Runs from cron every 30 min. Trigger: cash > $30,000 AND marker doesn't exist.
"""
import sys, os, time, datetime, json
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

MARKER = '/home/qbao775/serenity-trader-stack/.nz100k-deployed'
LOG = '/home/qbao775/serenity-trader-stack/auto-deploy.log'
THRESHOLD = 30000.0
USER_EMAIL = 'bqmbill714@gmail.com'

TARGETS = {
    'SPY':   0.70,
    'QQQ':   0.15,
    'BRK.B': 0.12,
    # cash: 0.03 (implicit)
}
SELL_LIST = {'NVDA', 'MU'}  # Small existing positions; sell to consolidate into Plan D

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def send_email(subject, body):
    key = os.environ.get('RESEND_API_KEY')
    if not key:
        log("email skipped: RESEND_API_KEY not set in env")
        return
    try:
        import requests
        r = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'from': 'onboarding@resend.dev', 'to': [USER_EMAIL],
                  'subject': subject, 'text': body}, timeout=15)
        log(f"email: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log(f"email err: {e}")

if os.path.exists(MARKER):
    log("marker exists → already deployed → exit")
    sys.exit(0)

from database import SessionLocal, get_setting
import alpaca_trade_api as tradeapi
import market_data as md

db = SessionLocal()
k = get_setting(db, 'alpaca_api_key', 1); s = get_setting(db, 'alpaca_secret_key', 1)
u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets'); db.close()
api = tradeapi.REST(k, s, u)

a = api.get_account()
cash = float(a.cash)
equity = float(a.equity)

if cash < THRESHOLD:
    log(f"cash=${cash:.2f} < ${THRESHOLD:.0f} → NZ$100k not arrived → exit")
    sys.exit(0)

log(f"🚀 cash=${cash:.2f} equity=${equity:.2f} → NZ$100k detected → Plan D deploy")

clock = api.get_clock()
mkt_open = clock.is_open

def get_price(sym):
    try:
        q = md.get_stock_quote(sym)
        if q and q.get('current'):
            return q['current']
    except Exception:
        pass
    # fallback approx prices
    return {'SPY': 746, 'QQQ': 511, 'BRK.B': 509, 'NVDA': 196, 'MU': 140}.get(sym, 100)

def submit_sell(sym, qty, current_px):
    if mkt_open:
        return api.submit_order(symbol=sym, qty=qty, side='sell',
                                type='market', time_in_force='day')
    else:
        limit = round(current_px * 0.992, 2)
        return api.submit_order(symbol=sym, qty=qty, side='sell',
                                type='limit', limit_price=limit,
                                time_in_force='day', extended_hours=True)

def submit_buy(sym, notional, current_px):
    if mkt_open:
        return api.submit_order(symbol=sym, notional=round(notional, 2),
                                side='buy', type='market', time_in_force='day')
    else:
        limit = round(current_px * 1.01, 2)
        qty = round(notional / limit, 4)
        return api.submit_order(symbol=sym, qty=qty, side='buy',
                                type='limit', limit_price=limit,
                                time_in_force='day', extended_hours=True)

try:
    # STEP 1: Sell NVDA + MU
    log("── STEP 1: sell NVDA + MU ──")
    sell_orders = []
    for p in api.list_positions():
        if p.symbol in SELL_LIST and float(p.market_value) >= 1:
            px = float(p.current_price)
            o = submit_sell(p.symbol, float(p.qty), px)
            log(f"  ✓ SELL {p.symbol} {float(p.qty)}sh @ ~${px} id={o.id[:8]}")
            sell_orders.append(o.id)

    if sell_orders:
        log("waiting 15s for sells to fill...")
        time.sleep(15)

    # Refresh account after sells
    a2 = api.get_account()
    total = float(a2.equity)
    bp = float(a2.buying_power)
    log(f"post-sell: equity=${total:.2f} bp=${bp:.2f}")

    # STEP 2: Buy targets sized to total account value
    log("── STEP 2: place Plan D buys ──")
    orders = []
    for sym, weight in TARGETS.items():
        target_notional = total * weight
        # For SPY, subtract the value of existing SPY position (already contributes)
        if sym == 'SPY':
            existing = sum(float(p.market_value) for p in api.list_positions()
                           if p.symbol == 'SPY')
            target_notional -= existing
            log(f"  {sym}: target ${total*weight:.0f}, existing ${existing:.2f}, "
                f"need to buy ${target_notional:.2f}")
        if target_notional < 10:
            log(f"  {sym}: skip (target ${target_notional:.2f} < $10)")
            continue
        # Cap at available buying power minus safety buffer
        target_notional = min(target_notional, bp - 100)
        px = get_price(sym)
        o = submit_buy(sym, target_notional, px)
        log(f"  ✓ BUY {sym} notional ${target_notional:.2f} @ ~${px} id={o.id[:8]}")
        orders.append(o.id)
        bp -= target_notional  # decrement local tally

    # Marker
    with open(MARKER, 'w') as f:
        json.dump({
            'deployed_at': datetime.datetime.utcnow().isoformat(),
            'cash_at_trigger': cash,
            'equity_at_trigger': total,
            'plan': 'D',
            'targets': TARGETS,
            'sell_orders': sell_orders,
            'buy_orders': orders,
        }, f, indent=2)
    log(f"✓ marker written → {MARKER}")

    # Email
    body = f"""Plan D 部署完成!

时间: {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC}
账户 equity 触发时: ${cash:.2f} cash → ${total:.2f} total

Plan D 目标配置:
  70% SPY   (~${total*0.70:.0f})
  15% QQQ   (~${total*0.15:.0f})
  12% BRK.B (~${total*0.12:.0f})
   3% cash  (~${total*0.03:.0f})

操作:
- 卖 NVDA + MU(整合到 Plan D)
- 买 SPY / QQQ / BRK.B 到目标比例

未来:
- 每 6 个月一次 /portfolio-review(下次 2027-01)
- 允许配置偏离目标 ±5pp;>5pp 我发提醒
- 你只需要每半年花 15 分钟看邮件汇总
"""
    send_email("✅ Plan D 部署完成 (Future-Aware Balanced)", body)
    log("─── DONE ───")

except Exception as e:
    log(f"✗ ERROR: {e}")
    send_email("⚠️ Plan D 部署失败",
        f"错误: {e}\ncash=${cash:.2f}\n请手动处理或叫醒我。")
    sys.exit(1)
