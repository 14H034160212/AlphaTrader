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
SKHY position (5% vs 20%) and initially added a self-imposed -15%
stop-loss. User then explicitly said "我觉得不需要设置止损" (no stop-loss
needed) -- removed. MU is now a pure hold with NO defined exit condition
at all (not even a take-profit target like SKHY's $200) -- the only thing
watching it is crossvalidate_satellite.py's regular 4h thesis recheck,
which can escalate/recommend TRIM/EXIT but does not auto-sell. This script
now only handles entry; there is no ongoing management logic left to run
after that.
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
# STOP_LOSS_PCT removed 2026-07-11 -- user: "我觉得不需要设置止损".
# No downside limit on this position at all now.


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
    state = {'entry_price_est': px, 'qty': qty}
    save_state(state)
    send_email("📈 MU 重新建仓",
               f"买入 MU {qty}股,预估入场价 ~${px}\n"
               f"不设止损、不设止盈目标(用户明确要求不设止损)\n"
               f"后续由 crossvalidate_satellite.py 的常规4小时论文复核自动跟踪,"
               f"该机制只会提示/升级,不会自动卖出。")


def main():
    if os.path.exists(DONE_MARKER):
        log("MU already entered — no stop-loss, no take-profit target, nothing left for this "
            "script to do. Ongoing monitoring is crossvalidate_satellite.py's job.")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    log("no MU position yet — attempting entry")
    enter_position(api)


if __name__ == '__main__':
    main()
