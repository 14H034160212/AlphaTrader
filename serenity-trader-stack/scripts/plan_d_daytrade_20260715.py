#!/usr/bin/env python3
"""
plan_d_daytrade_20260715.py — ONE-OFF day-trade, 2026-07-15 ONLY, using
Plan D's own target names (SPY/QQQ) as today's picks, NOT a real Plan D
re-entry (which would be a long-term hold).

Context: reentry_monitor.py's 4-gate check for a genuine Plan D re-entry
showed gate [1] (regime streak) passed (5/5 RISK_ON days) but gate [2]
(Korea stability) FAILED -- KOSPI/KOSDAQ just triggered a fresh sell
sidecar and are retesting the 2026-07-08 crash lows (same Hormuz/Iran
escalation this whole week's trading has been reacting to). Claude flagged
this explicitly before acting; user said **"明确跳过这个条件，就买今天一天"**
(explicitly skip that condition, just buy for today) -- confirmed via a
structured choice to mean a SAME-DAY TRADE (sell everything before close),
NOT an actual long-term Plan D re-entry. This is a real, deliberate
distinction: the account is NOT re-entering Plan D today; it's using Plan
D's names for one more day-trade in the same pattern as NOC/META/MU/SNDK/
SKHY earlier this week.

Allocation: user said **"可以把SPY和QQQ多加一些，BRK.b可以不买"** (add more to
SPY/QQQ, skip BRK.B), then when the plan still showed a 3% cash buffer,
explicitly said **"不要留"** (don't keep any back) and **"尽量都买标普500和
qqq"** (put as much as possible into SPY/QQQ) -- so this is now a full
deployment, no cash reserve at all: SPY ~83%, QQQ ~17% (same relative
70:15 tilt as Plan D's original weights, just scaled up to use 100% instead
of leaving Plan D's usual 3% cash slice). All SGOV sold to fund this.

Rules (identical to the FINAL settled rules from bull_day_trade_20260714.py
after several rounds of live correction that day -- start from the mature
version, don't re-litigate):
  - NO early profit-take / no downtrend-confirmed exit -- ride out normal
    intraday noise, this is index-level, lower-volatility names anyway.
  - STOP_LOSS_PCT: -3.0% (tighter than the -4% used for individual
    momentum stocks earlier this week -- SPY/QQQ are index-level and far
    less volatile, so a smaller adverse move here is more meaningful and a
    -4% floor would be needlessly loose for this basket).
  - Mandatory close-out ~15min before market close regardless of P&L.
  - No re-entry cycle (unlike bull_day_trade_20260714.py) -- this is a
    single overnight-risk-driven trade, not meant to be repeatedly rebought
    all day; once stopped out or closed at end of day, stays flat.

This does NOT touch the actual Plan D re-entry criteria/reentry_monitor.py
-- that script's own gates remain unmet and it stays paused; tomorrow, if
Korea stabilizes, an actual long-term re-entry would still go through the
normal confirmed process, separate from this one-off trade.
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

WEIGHTS = {'SPY': 0.83, 'QQQ': 0.17}  # no cash reserve -- user: "不要留" / "尽量都买标普500和qqq"
STOP_LOSS_PCT = -3.0
STATE_FILE = '/home/qbao775/serenity-trader-stack/.plan_d_daytrade_20260715_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.plan_d_daytrade_20260715_done'


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
    from email.mime.text import MIMEText
    from database import SessionLocal, get_setting
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: not configured")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recip
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw)
        s.sendmail(sender, [recip], msg.as_string())
        s.quit()
        log(f"email sent to {recip}")
    except Exception as e:
        log(f"email err: {e}")


def enter(api, state):
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)

    for sym, w in WEIGHTS.items():
        if state.get(sym, {}).get('entered'):
            continue
        notional = min(equity * w, bp - 20)
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            log(f"  {sym}: no live price yet — will retry next tick")
            continue
        qty = round(notional / px, 4)
        if qty <= 0:
            log(f"  {sym}: insufficient buying power — skipping")
            continue
        o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
        log(f"  ✓ BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]}")
        state[sym] = {'entered': True}
        bp -= notional
        save_state(state)

    if all(state.get(sym, {}).get('entered') for sym in WEIGHTS):
        send_email("📈 Plan D 名称短线建仓 (仅今天,非长期入场)",
                   "已按 SPY 80% / QQQ 17% 买入(BRK.B按你的要求跳过)。\n"
                   "这不是Plan D正式重新入场——韩国稳定性那条门槛还没过,"
                   "这只是借用Plan D的标的做今天一天的短线,收盘前会全部卖出。")


def manage(api, state):
    positions = {p.symbol: p for p in api.list_positions() if p.symbol in WEIGHTS}
    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60

    for sym in WEIGHTS:
        if sym not in positions:
            continue
        p = positions[sym]
        current_px = float(p.market_value) / float(p.qty)
        plpc = float(p.unrealized_plpc) * 100
        log(f"  {sym}: qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}%")

        reason = None
        if plpc <= STOP_LOSS_PCT:
            reason = f"亏损达到 {plpc:+.2f}% (<= {STOP_LOSS_PCT}%),止损离场"
        elif mins_to_close <= 15:
            reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%),按规则强制平仓"

        if not reason:
            continue

        o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
        log(f"  ✓ SOLD {sym} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
        state.setdefault(sym, {})['closed'] = True
        send_email(f"{'🎯' if plpc > 0 else '🛑'} {sym} 短线交易平仓",
                   f"卖出价 ~${current_px:.2f}\n最终盈亏: {plpc:+.2f}%\n原因: {reason}")

    save_state(state)

    still_held = {p.symbol for p in api.list_positions() if p.symbol in WEIGHTS}
    if mins_to_close <= 15 and not still_held and all(state.get(s, {}).get('entered') for s in WEIGHTS):
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        log("today's Plan-D-names day-trade fully wound down — marking done")


def main():
    if os.path.exists(DONE_MARKER):
        log("today's trading already done — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    if not all(state.get(sym, {}).get('entered') for sym in WEIGHTS):
        enter(api, state)
    else:
        manage(api, state)


if __name__ == '__main__':
    main()
