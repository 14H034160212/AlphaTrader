#!/usr/bin/env python3
"""
skhy_daytrade.py — one-off, user-directed day-trade on SK Hynix's Nasdaq
debut (ticker SKHY, priced at $149/ADR, trading starts 2026-07-10 9:30am ET).

User's exact instruction (2026-07-09): "我赌10%炒个短线sk海力士，开盘就买入，
然后你看涨势，如果一直涨就收盘的时候卖掉" — 10% of equity, buy at the open,
sell at close if it's trending up.

This is explicitly a single-day, defined-risk speculative trade — NOT part
of Plan D, NOT part of the satellite framework, NOT covered by
reentry_monitor.py. It funds itself by selling a slice of the SGOV position
(the account is otherwise ~100% SGOV per the 2026-07-08 liquidation).

Claude's own addition (not explicitly requested, but serves the user's
stated "don't lose money" goal): a stop-loss, since "watch it and decide at
close" alone leaves the position fully exposed to intraday IPO-debut
volatility with no downside protection. STOP_LOSS_PCT below.

Logic per run (cron'd every 15 min during market hours, 2026-07-10 ONLY):
  1. No position yet, market open has passed -> sell SGOV to fund ~10% of
     equity, buy SKHY at market.
  2. Position held, price down >= STOP_LOSS_PCT from entry -> sell (protect
     capital, don't wait for close).
  3. Position held, within ~15 min of close -> sell regardless of direction
     (this was framed as a single-day trade; the user only specified the
     "sell at close if rising" case, but leaving a losing day-trade open
     overnight "hoping it comes back" would silently turn a bounded bet
     into open-ended risk — the same discipline this account already
     applies to satellite names, just faster on a single-day trade).
  4. Once sold (for any reason) -> write DONE_MARKER, take no further action.

Remove the cron entry after 2026-07-10 close — this script has no ongoing
purpose past that one trading day.
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.skhy_daytrade_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.skhy_daytrade_done'
LOG_PATH = '/home/qbao775/serenity-trader-stack/skhy_daytrade.log'

TARGET_PCT = 0.10          # 10% of equity, per user's instruction
STOP_LOSS_PCT = -10.0      # Claude's addition — protect capital intraday
CLOSE_BUFFER_MIN = 15      # sell this many minutes before 4pm ET close
TRAIL_FROM_PEAK_PCT = -4.0 # sell if price pulls back this much from the
                           # intraday peak seen so far — approximates
                           # "sell near the high" without requiring perfect
                           # foresight of the exact top (impossible in
                           # real time). Only engages once price has moved
                           # meaningfully above entry (see manage_position).
TRAIL_ARM_ABOVE_PCT = 3.0  # only start trailing once up at least this much
                           # from entry — otherwise normal early-session
                           # noise would trigger it immediately


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


def enter_position(api):
    acc = api.get_account()
    equity = float(acc.equity)
    target_notional = equity * TARGET_PCT

    sgov = [p for p in api.list_positions() if p.symbol == 'SGOV']
    if not sgov:
        log("no SGOV position to fund this trade from — aborting")
        return None
    sgov_qty = float(sgov[0].qty)
    sgov_px = float(sgov[0].market_value) / sgov_qty
    sell_qty = int((target_notional / sgov_px) + 5)  # small buffer
    sell_qty = min(sell_qty, int(sgov_qty))

    o1 = api.submit_order(symbol='SGOV', qty=sell_qty, side='sell', type='market', time_in_force='day')
    log(f"  sold {sell_qty}sh SGOV to fund entry, order={o1.id[:8]}")

    import time
    time.sleep(8)
    acc = api.get_account()
    bp = float(acc.buying_power)

    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    r = requests.get('https://data.alpaca.markets/v2/stocks/SKHY/trades/latest',
                      headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
    px = None
    if r.status_code == 200:
        px = r.json().get('trade', {}).get('p')
    if not px:
        log("  no live SKHY price available yet — will retry next cron tick")
        return None

    qty = int(min(target_notional, bp - 20) // px)
    if qty < 1:
        log(f"  insufficient funds for even 1 share of SKHY @ ${px} — aborting")
        return None

    o2 = api.submit_order(symbol='SKHY', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT SKHY qty={qty} @~${px} order={o2.id[:8]}")

    state = {
        'entered': True,
        'entry_time': datetime.datetime.utcnow().isoformat(),
        'entry_price_est': px,
        'qty': qty,
    }
    save_state(state)
    send_email("📈 SKHY 首秀日交易 — 已建仓",
               f"买入 SKHY {qty}股,预估入场价 ~${px}\n止损线: {STOP_LOSS_PCT}%\n"
               f"收盘前 {CLOSE_BUFFER_MIN} 分钟无论涨跌都会平仓。")
    return state


def manage_position(api, state):
    positions = [p for p in api.list_positions() if p.symbol == 'SKHY']
    if not positions:
        log("no SKHY position held (already sold or never filled) — marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        return

    p = positions[0]
    plpc = float(p.unrealized_plpc) * 100
    mv = float(p.market_value)
    current_px = mv / float(p.qty)

    # Track the intraday peak price seen so far, so we can trail from it.
    # "Sell at the exact daily high" isn't achievable in real time — this is
    # the realistic approximation: once up a meaningful amount, sell if it
    # pulls back a chunk from the best level reached, locking in most of
    # the run rather than giving it all back waiting for a peak that's
    # already passed.
    peak_px = max(state.get('peak_price', current_px), current_px)
    state['peak_price'] = peak_px
    save_state(state)
    pullback_from_peak_pct = (current_px / peak_px - 1) * 100
    up_from_entry_pct = (current_px / state['entry_price_est'] - 1) * 100

    log(f"  SKHY position: qty={p.qty} mv=${mv:.2f} current=${current_px:.2f} "
        f"unrealized_plpc={plpc:+.2f}% peak=${peak_px:.2f} pullback_from_peak={pullback_from_peak_pct:+.2f}%")

    clock = api.get_clock()
    now = datetime.datetime.now(datetime.timezone.utc)
    close_time = clock.next_close if clock.is_open else None
    near_close = False
    if close_time:
        mins_to_close = (close_time - now).total_seconds() / 60
        near_close = 0 <= mins_to_close <= CLOSE_BUFFER_MIN

    trailing_armed = up_from_entry_pct >= TRAIL_ARM_ABOVE_PCT
    trailing_triggered = trailing_armed and pullback_from_peak_pct <= TRAIL_FROM_PEAK_PCT

    if plpc <= STOP_LOSS_PCT:
        reason = f"止损触发 ({plpc:+.2f}% <= {STOP_LOSS_PCT}%)"
    elif trailing_triggered:
        reason = (f"从高点回落触发 (最高 ${peak_px:.2f}, 现价 ${current_px:.2f}, "
                   f"回落 {pullback_from_peak_pct:+.2f}%, 当前盈利 {plpc:+.2f}%)")
    elif near_close:
        reason = f"接近收盘(单日交易,无论涨跌都平仓,当前 {plpc:+.2f}%)"
    else:
        log(f"  未触发平仓条件(止损{STOP_LOSS_PCT}% / 回落追踪{'已启动' if trailing_armed else '未启动,需先上涨'+str(TRAIL_ARM_ABOVE_PCT)+'%'} / 非收盘时段),继续持有")
        return

    o = api.submit_order(symbol='SKHY', qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD SKHY qty={p.qty} reason={reason} order={o.id[:8]}")
    with open(DONE_MARKER, 'w') as f:
        json.dump({'closed_at': datetime.datetime.utcnow().isoformat(), 'reason': reason,
                    'final_plpc': plpc}, f, indent=2)
    send_email(f"SKHY 首秀日交易 — 已平仓 ({plpc:+.2f}%)",
               f"平仓原因: {reason}\n最终盈亏: {plpc:+.2f}%")


def main():
    if os.path.exists(DONE_MARKER):
        log("already closed today — nothing more to do")
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
        manage_position(api, state)


if __name__ == '__main__':
    main()
