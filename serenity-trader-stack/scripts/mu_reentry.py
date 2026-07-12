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

# 2026-07-11: user asked to watch first and prefer a pullback before buying
# ("你可以先看一下，最好等回落一些再买"), then added: "上周五冲高回落，有资金
# 在借利好卖出，要等回落走稳了再买" -- some holders are selling into the good
# news, so wait for the price to actually STABILIZE, not just bounce once.
# Approximation, not a guarantee: require (a) price off its observed low by
# some margin AND (b) no new low in the last few checks (proxy for "selling
# pressure has actually let up"), or a max wait so this doesn't wait forever.
ENTRY_RECOVERY_FROM_LOW_PCT = 1.5
ENTRY_STABLE_CHECKS_REQUIRED = 2   # consecutive checks with no new low
ENTRY_MAX_WAIT_MIN = 120


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
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from database import SessionLocal, get_setting
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: email_sender/email_app_password/email_recipient not set in DB")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recip
        msg.attach(MIMEText(body, "plain"))
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw)
        s.sendmail(sender, [recip], msg.as_string())
        s.quit()
        log(f"email sent to {recip}")
    except Exception as e:
        log(f"email err: {e}")


def enter_position(api, state):
    import market_data as md
    q = md.get_stock_quote('MU')
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live MU price available yet — will retry next tick")
        return

    now = datetime.datetime.utcnow()
    watch = state.get('watch')
    if not watch:
        state['watch'] = {'first_seen_time': now.isoformat(), 'lowest_price': px, 'stable_checks': 0}
        save_state(state)
        log(f"  first price observed (${px:.2f}) — watching for stabilization, not chasing the open")
        return

    made_new_low = px < watch['lowest_price']
    watch['lowest_price'] = min(watch['lowest_price'], px)
    watch['stable_checks'] = 0 if made_new_low else watch['stable_checks'] + 1

    first_time = datetime.datetime.fromisoformat(watch['first_seen_time'])
    mins_waiting = (now - first_time).total_seconds() / 60
    recovery_pct = (px / watch['lowest_price'] - 1) * 100

    recovered_and_stable = (recovery_pct >= ENTRY_RECOVERY_FROM_LOW_PCT
                             and watch['stable_checks'] >= ENTRY_STABLE_CHECKS_REQUIRED)
    timed_out = mins_waiting >= ENTRY_MAX_WAIT_MIN
    if not (recovered_and_stable or timed_out):
        state['watch'] = watch
        save_state(state)
        log(f"  watching: current=${px:.2f} low=${watch['lowest_price']:.2f} recovery={recovery_pct:+.2f}% "
            f"stable_checks={watch['stable_checks']}/{ENTRY_STABLE_CHECKS_REQUIRED} "
            f"waited={mins_waiting:.0f}/{ENTRY_MAX_WAIT_MIN}min — not buying yet")
        return

    reason = "recovered and stabilized off the observed low" if recovered_and_stable else f"max wait ({ENTRY_MAX_WAIT_MIN}min) elapsed, buying regardless"
    log(f"  entry condition met ({reason}) — buying now")

    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    qty = round(min(target_notional, bp - 20) / px, 4)
    if qty <= 0:
        log(f"  insufficient buying power for MU @ ${px} — aborting")
        return

    o = api.submit_order(symbol='MU', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT MU qty={qty} @~${px} order={o.id[:8]}")

    with open(DONE_MARKER, 'w') as f:
        json.dump({'entered_at': datetime.datetime.utcnow().isoformat()}, f)
    save_state({'entry_price_est': px, 'qty': qty})
    send_email("📈 MU 重新建仓",
               f"买入 MU {qty}股,预估入场价 ~${px}(等待理由: {reason})\n"
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

    state = load_state()
    log("no MU position yet — attempting entry")
    enter_position(api, state)


if __name__ == '__main__':
    main()
