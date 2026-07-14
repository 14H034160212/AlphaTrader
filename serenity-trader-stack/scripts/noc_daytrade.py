#!/usr/bin/env python3
"""
noc_daytrade.py — ONE-OFF day-trade, 2026-07-14 ONLY. Rules went through
several rapid revisions in one live conversation -- final state (see full
sequence below) is: exit the instant there's ANY real profit (>= +0.1%,
user's own words), don't wait for a bigger move and don't wait for close
either; stop-loss and mandatory close-out remain as the backstops.

Full instruction sequence, in order:
  1. "美股今天好像很不错，可不可以那10%的钱出来买你觉得合适的股票" -- take
     10% out, buy something appropriate, Claude's pick.
  2. "然后你赚钱就马上出来，今天收盘之前全部卖掉" -- take profit immediately,
     also sell everything before close regardless.
  3. "不一定1%，不管涨多少，收盘全部卖掉" -- (walked back #2's early exit)
     don't need a specific %, just hold to close no matter how far up.
  4. "也不一定等到收盘再全部卖你觉得涨的差不多能赚钱就卖" -- (walked back #3)
     don't necessarily wait for close either -- Claude's own judgment on
     "risen enough for a decent profit".
  5. "我要确保今天哪怕赚0.1%也可以" -- (sharpened #4 into a concrete number)
     even a mere +0.1% profit today is fine to take.
Net effect of 2-5 together: take profit at almost any positive move, don't
hold out for a bigger one, don't wait for close either -- back to an early
exit like step 2, just with a much lower bar than a first-draft 1% would
have been.

This IS the explicit per-buy confirmation the 2026-07-13 policy requires —
the user is directly instructing a buy in live conversation, delegating only
the SPECIFIC pick to Claude's judgment, not inferring an unstated buy.

Pick: NOC (Northrop Grumman). Reasoning from same-day conversation, not a
random "market's up" chase: in a same-conversation comparison of
oil/gold/silver/defense positioning (triggered by the Iran/Hormuz
escalation), NOC was the one name sitting near its 52-week LOW (17% of
range, PE ~16.9x) while its defense peers (GD 91%, RTX 74%, ITA 73% of
range, PE 23-37x) were trading near their highs -- a genuine laggard in a
sector with a live, real catalyst (Gulf conflict), not hype. Small Monday
move (+0.46%) means it isn't being chased into this trade either.

Rules (day-trade, NOT a long-term hold like SKHY/MU/META):
  1. Buy ~10% of equity at/near today's open.
  2. PROFIT_EXIT_PCT = 0.1 -- exit the instant unrealized P&L clears this
     (user's literal words: even +0.1% is fine to take). Deliberately NOT
     zero -- needs to clear real bid/ask spread + fees, not just a noise
     print, but otherwise as low a bar as the user asked for.
  3. STOP_LOSS_PCT: -5% -- not explicitly requested, but added on the same
     judgment basis as the 2026-07-10 SKHY day-trade (serves the user's own
     implicit "don't lose money" goal for an unsupervised same-day trade).
  4. Mandatory close-out ~15min before market close regardless of P&L --
     this is a single-day trade, never held overnight.

Cron: scoped to 2026-07-14 ONLY (day=14, month=7 in the spec) -- remove the
cron entries after today's close; this script has no purpose beyond today.
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

SYMBOL = 'NOC'
TARGET_PCT = 0.10
# 2026-07-14: went 1.0% -> removed -> back to a number, ending at 0.1% per
# user's own words "我要确保今天哪怕赚0.1%也可以" (make sure even +0.1% profit
# today is fine to take) -- exit on almost any real gain, don't hold out.
PROFIT_EXIT_PCT = 0.1
STOP_LOSS_PCT = -5.0
STATE_FILE = '/home/qbao775/serenity-trader-stack/.noc_daytrade_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.noc_daytrade_done'


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
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    # Bug fix (2026-07-14, caught live): most of this account's cash was
    # parked in SGOV that same morning, so buying_power alone came up far
    # short of the intended 10% -- the first version of this function
    # silently capped the buy to whatever cash happened to be free instead
    # of raising the rest, under-sizing the trade to ~1% instead of 10%.
    # Sell enough SGOV first to cover the shortfall, same pattern
    # reentry_monitor.py/skhy_daytrade.py already use to fund from SGOV.
    if bp < target_notional:
        shortfall = target_notional - bp
        sgov = [p for p in api.list_positions() if p.symbol == 'SGOV']
        if sgov:
            import requests
            from database import SessionLocal, get_setting
            db = SessionLocal()
            k = get_setting(db, 'alpaca_api_key', 1); s = get_setting(db, 'alpaca_secret_key', 1)
            db.close()
            r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/trades/latest',
                              headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
            sgov_px = r.json().get('trade', {}).get('p')
            if sgov_px:
                sell_qty = min(int(sgov[0].qty), int((shortfall + 30) // sgov_px) + 1)
                if sell_qty > 0:
                    o = api.submit_order(symbol='SGOV', qty=sell_qty, side='sell', type='market', time_in_force='day')
                    log(f"  sold {sell_qty}sh SGOV to cover the ~${shortfall:.2f} shortfall order={o.id[:8]}")
                    import time; time.sleep(10)
                    acc = api.get_account()
                    bp = float(acc.buying_power)

    target_notional = min(target_notional, bp - 20)

    import market_data as md
    q = md.get_stock_quote(SYMBOL)
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live price for NOC yet — will retry next tick")
        return

    qty = round(target_notional / px, 4)
    if qty <= 0:
        log("  insufficient buying power — aborting")
        return

    o = api.submit_order(symbol=SYMBOL, qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT {SYMBOL} qty={qty} @~${px} order={o.id[:8]}")
    state['entered'] = True
    state['entry_price_est'] = px
    state['qty'] = qty
    save_state(state)
    send_email("📈 NOC 日内交易 — 已建仓",
               f"买入 NOC {qty}股,预估入场价 ~${px:.2f}(约占总仓位10%)\n"
               f"规则: 只做今天短线,浮盈超过{PROFIT_EXIT_PCT}%就立即卖出(哪怕只赚一点点);"
               f"止损{STOP_LOSS_PCT}%;无论盈亏,收盘前15分钟强制平仓。")


def manage(api, state):
    positions = [p for p in api.list_positions() if p.symbol == SYMBOL]
    if not positions:
        log(f"no {SYMBOL} position held (already sold or never filled) — marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        return

    p = positions[0]
    current_px = float(p.market_value) / float(p.qty)
    plpc = float(p.unrealized_plpc) * 100
    log(f"  {SYMBOL} position: qty={p.qty} current=${current_px:.2f} unrealized_plpc={plpc:+.2f}%")

    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60 \
        if clock.is_open else 0

    reason = None
    if plpc >= PROFIT_EXIT_PCT:
        reason = f"浮盈 {plpc:+.2f}% (>= {PROFIT_EXIT_PCT}%),按规则立即止盈离场(哪怕只赚一点点)"
    elif plpc <= STOP_LOSS_PCT:
        reason = f"亏损达到 {plpc:+.2f}% (<= {STOP_LOSS_PCT}%),止损"
    elif clock.is_open and mins_to_close <= 15:
        reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%),按规则强制平仓"

    if not reason:
        log(f"  未触发退出条件,继续持有 (还剩{mins_to_close:.0f}分钟到收盘)")
        return

    o = api.submit_order(symbol=SYMBOL, qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD {SYMBOL} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
    with open(DONE_MARKER, 'w') as f:
        json.dump({'closed_at': datetime.datetime.utcnow().isoformat(),
                    'exit_price': current_px, 'final_plpc': plpc, 'reason': reason}, f, indent=2)
    send_email(f"{'🎯' if plpc > 0 else '🛑'} NOC 日内交易平仓",
               f"卖出价 ~${current_px:.2f}\n最终盈亏: {plpc:+.2f}%\n原因: {reason}")


def main():
    if os.path.exists(DONE_MARKER):
        log("already closed out today — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    if not state.get('entered'):
        log("no NOC position yet — entering")
        enter(api, state)
    else:
        manage(api, state)


if __name__ == '__main__':
    main()
