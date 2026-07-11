#!/usr/bin/env python3
"""
skhy_position.py — longer-term SK Hynix (SKHY, regular ticker from
2026-07-13) position, per explicit user instruction (2026-07-11):

"接下来你可以等下周开始卖海力士，但是不要因为跌就卖掉，我觉得你可以拿住
然后等超过200在卖" — buy at Monday's open, do NOT sell on weakness, hold
regardless of drawdown, only sell once price >= $200.

Claude asked to confirm whether this meant literally no downside limit at
all vs. a wide catastrophe-only stop. User's answer: "不设任何下线" — no
downside limit whatsoever. This is DELIBERATELY different from
skhy_daytrade.py's tight stop-loss/trailing-stop/close-out discipline —
this is a genuine buy-and-hold position with a single take-profit target,
not a day-trade. No stop-loss means real, uncapped downside exposure is
accepted on purpose here; do not add one back in without the user asking.

Logic per run (cron'd periodically, NOT scoped to a single day — this
persists until the target is hit):
  1. No position yet, market open, ticker SKHY has live data -> buy with
     the allocated notional (~20% of equity, same sizing as the prior
     day-trade, recomputed against current equity at execution time).
  2. Position held, price >= TARGET_PRICE -> sell, done.
  3. Position held, price < TARGET_PRICE -> hold, no matter how far down.
     No stop-loss. No time limit. This is intentional.

Remove SATELLITE_BUYING_PAUSED-style marker only when sold or if the user
explicitly changes the plan.
"""
import sys, os, json, datetime
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

STATE_FILE = '/home/qbao775/serenity-trader-stack/.skhy_position_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.skhy_position_done'

TARGET_PCT = 0.20        # same 20% sizing as the day-trade, user hasn't said otherwise
TARGET_PRICE = 200.0     # sell ONLY when price >= this. No other exit condition.


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def get_alpaca():
    from database import SessionLocal, get_setting
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    return tradeapi.REST(k, s, u)


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
            json={'from': 'onboarding@resend.dev', 'to': ['bqmbill714@gmail.com'],
                  'subject': subject, 'text': body}, timeout=15)
        log(f"email: {r.status_code}")
    except Exception as e:
        log(f"email err: {e}")


def get_live_price(api, symbol):
    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    r = requests.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest',
                      headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
    if r.status_code == 200:
        return r.json().get('trade', {}).get('p')
    return None


def enter_position(api):
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    px = get_live_price(api, 'SKHY')
    if not px:
        log("  no live SKHY price yet — will retry next tick")
        return

    qty = int(min(target_notional, bp - 20) // px)
    if qty < 1:
        log(f"  insufficient buying power for even 1 share of SKHY @ ${px} — aborting")
        return

    o = api.submit_order(symbol='SKHY', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT SKHY qty={qty} @~${px} order={o.id[:8]}")

    state = {'entered': True, 'entry_time': datetime.datetime.utcnow().isoformat(),
             'entry_price_est': px, 'qty': qty}
    save_state(state)
    send_email("📈 SKHY 长期持仓 — 已建仓",
               f"买入 SKHY {qty}股,预估入场价 ~${px}\n"
               f"目标价: ${TARGET_PRICE} 才卖出,中途不设止损,跌多少都不动。")


def manage_position(api):
    positions = [p for p in api.list_positions() if p.symbol == 'SKHY']
    if not positions:
        log("no SKHY position held (already sold or never filled) — marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        return

    p = positions[0]
    current_px = float(p.market_value) / float(p.qty)
    plpc = float(p.unrealized_plpc) * 100
    log(f"  SKHY position: qty={p.qty} current=${current_px:.2f} unrealized_plpc={plpc:+.2f}% "
        f"(target=${TARGET_PRICE}, no stop-loss by design)")

    if current_px >= TARGET_PRICE:
        o = api.submit_order(symbol='SKHY', qty=p.qty, side='sell', type='market', time_in_force='day')
        log(f"  ✓ TARGET HIT — SOLD SKHY qty={p.qty} @~${current_px:.2f} order={o.id[:8]}")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat(),
                        'exit_price': current_px, 'final_plpc': plpc}, f, indent=2)
        send_email(f"🎯 SKHY 达到目标价 ${TARGET_PRICE} — 已卖出",
                   f"卖出价 ~${current_px:.2f}\n最终盈亏: {plpc:+.2f}%")
    else:
        log(f"  未到目标价,继续持有(不设止损,这是刻意的设计)")


def main():
    if os.path.exists(DONE_MARKER):
        log("already sold (target hit) — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    if not state.get('entered'):
        log("no position yet — attempting entry")
        enter_position(api)
    else:
        manage_position(api)


if __name__ == '__main__':
    main()
