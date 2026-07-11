#!/usr/bin/env python3
"""
mu_reentry.py — re-enter MU (Micron), user-directed (2026-07-11): "可以都买，
你决定" (buy both MU and SKHY, size at Claude's discretion).

Important context Claude flagged before executing: MU was explicitly
REJECTED on 2026-07-02 during a systematic screen as a classic memory-cycle
valuation trap (cheap forward PE reflecting peak-cycle earnings that
historically mean-revert once supply catches up) — see
~/serenity-trader-stack/PLAN_D.md and project_management_mandate.md memory.
The 2026-07-09/11 news (Micron's $3B US supply-chain investment, Trump's
$250B figure, BofA/UBS bullish reiterations, DRAM pricing forecast raised
17%->32% QoQ) is real, but doesn't resolve that original valuation-trap
concern -- it's the same "AI demand is structural not cyclical" narrative
in a new news wrapper. This is a considered REVERSAL of a prior rejection,
not a fresh uncontested thesis.

Given that unresolved risk, Claude sized this more conservatively than the
SKHY position (5% vs 20%) and added a real stop-loss (-15%) -- unlike
SKHY, the user did NOT ask for "no downside limit" on this one, so this
reflects Claude's own risk-management judgment for a reversal-of-rejected-
thesis position, not an explicit instruction either way.

No fixed take-profit target (none was specified) -- once held, MU will
automatically show up in crossvalidate_satellite.py's get_satellite_positions()
and get the same 4-master + Serenity chokepoint hybrid recheck every 4h as
CRDO/TER/RRX. This script only handles entry + the stop-loss; ongoing
qualitative thesis monitoring is the existing satellite infrastructure's job.
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.mu_reentry_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.mu_reentry_entered'

TARGET_PCT = 0.05         # 5% -- more conservative than SKHY's 20%, given
                          # the unresolved valuation-trap concern
STOP_LOSS_PCT = -15.0     # Claude's own risk-management addition (not
                          # explicitly requested) -- wider than a day-trade
                          # stop since this is meant as a real hold, but a
                          # real limit given the thesis is a reversal of a
                          # prior rejection, not a fresh clean call


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
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    q = md.get_stock_quote('MU')
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live MU price available yet — will retry next tick")
        return

    qty = round(min(target_notional, bp - 20) / px, 4)
    if qty <= 0:
        log(f"  insufficient buying power for MU @ ${px} — aborting")
        return

    o = api.submit_order(symbol='MU', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT MU qty={qty} @~${px} order={o.id[:8]}")

    with open(DONE_MARKER, 'w') as f:
        json.dump({'entered_at': datetime.datetime.utcnow().isoformat()}, f)
    state = {'entry_price_est': px, 'qty': qty, 'stopped_out': False}
    save_state(state)
    send_email("📈 MU 重新建仓",
               f"买入 MU {qty}股,预估入场价 ~${px}\n"
               f"止损线: {STOP_LOSS_PCT}%(Claude 自行设置,用户未明确要求)\n"
               f"无固定止盈目标 — 后续由 crossvalidate_satellite.py 的常规4小时"
               f"论文复核自动跟踪。")


def check_stop_loss(api):
    positions = [p for p in api.list_positions() if p.symbol == 'MU']
    if not positions:
        return
    p = positions[0]
    plpc = float(p.unrealized_plpc) * 100
    log(f"  MU position: qty={p.qty} unrealized_plpc={plpc:+.2f}% (stop-loss {STOP_LOSS_PCT}%)")
    if plpc <= STOP_LOSS_PCT:
        o = api.submit_order(symbol='MU', qty=p.qty, side='sell', type='market', time_in_force='day')
        log(f"  ✓ STOP-LOSS TRIGGERED — SOLD MU qty={p.qty} @ {plpc:+.2f}% order={o.id[:8]}")
        send_email(f"⚠️ MU 止损触发 ({plpc:+.2f}%)", f"已按 -15% 止损线卖出。")


def main():
    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    if not os.path.exists(DONE_MARKER):
        log("no MU position yet — attempting entry")
        enter_position(api)
    else:
        check_stop_loss(api)


if __name__ == '__main__':
    main()
