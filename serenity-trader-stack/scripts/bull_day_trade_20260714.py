#!/usr/bin/env python3
"""
bull_day_trade_20260714.py — ONE-OFF day-trade, 2026-07-14 ONLY, covering
META/MU/SNDK. Full context (long, rapid-fire live conversation):

  1. "你做决定，反正就做短线今天不管如何要尽可能多的赚钱，然后全部卖掉离场，
     包括meta，海力士，美光，闪迪" -- you decide, it's day-trading anyway, no
     matter what today try to earn as much as possible, then sell everything
     and exit -- including META, SK Hynix (SKHY), Micron (MU), SanDisk (SNDK).
  2. "你让K线分析模型帮你分析一下...提一些建议你再买，包括什么时候买什么时候卖"
     -- have the existing analysis models look at it first, then decide
     entry/exit timing.
  3. "亏钱不要来找我，今天牛市要用好" -- explicit risk acceptance, don't hold
     back out of fear of blame; make good use of today's bull run.
  4. "我要睡觉了，就全权交给你把握了" -- going to sleep, full autonomous
     authority handed over for the rest of today.

This is a SCOPED, DATED exception to the 2026-07-13 standing "confirm before
any buy" policy (project_management_mandate.md / PLAN_D.md) -- it does NOT
reopen autonomous buying generally. It expires with today's close; tomorrow
the standing confirm-before-buy rule is back in force for everything,
including these same four names' long-term entry scripts.

Why SKHY was excluded: ran the same 4-master + Serenity quick-check used for
new candidates on all four names given today's price action. META came back
BULLISH/BULLISH with no long-term red flag (real fundamentals, not extended
today). MU and SNDK came back BEARISH-fundamentals/BULLISH-momentum -- the
same well-established memory-cycle valuation-trap concern already on record
for MU, momentum-only, but fine for a same-day trade since it's explicitly
NOT a long-term hold today. SKHY was the most extended (+10.6%, still making
new highs all day, zero pullback) and Serenity explicitly flagged
"CHOKEPOINT_INTACT: BROKEN" -- a pure momentum breakout with no fundamental
change, the textbook chase this account's discipline exists to avoid. Sizing
follows conviction: META 8%, MU 5%, SNDK 4% (~17% of equity total, spread
across three names rather than concentrated).

This is entirely SEPARATE from skhy_position.py/mu_reentry.py/meta_longhold.py
(the LONG-TERM entry scripts for these same tickers) -- those remain paused,
requiring their own .ENTRY_CONFIRMED_<NAME> file, untouched by this script.
No double-buy risk: this script manages only the shares it itself bought.

Exit rules -- REVISED THREE TIMES same day. (1) Original, learned from the
earlier NOC feedback ("这么快吗？你可以多看看，不要听我"): armed a trailing-
stop at +2%, exited on a >=1.5% pullback from peak. (2) User then said:
"我觉得要如果涨了超过2%可以不要设限，能多涨更好" (once up more than 2%, don't
cap it -- the more it rises the better) -- removed the trailing-stop
entirely. (3) User then clarified further: "我觉得只要从最高点开始下跌趋势就
马上卖了" (as soon as it starts a downtrend from its peak, sell immediately),
"不要等到亏钱再卖" (don't wait until it's actually losing money to sell) --
this brings a trailing exit BACK, but trend-confirmed rather than a fixed
percentage pullback: sell on the first CONFIRMED reversal off the peak
(2 consecutive declining checks, not a single noisy tick), independent of
whether P&L is still positive or has gone negative -- the point is to not
give back a peak, not to hit a specific number first.
Final rules:
  - DOWNTREND_CONFIRM_TICKS = 2: once a new peak P&L% is set, if the next 2
    checks in a row both come in lower than the one before, that's a
    confirmed downtrend off the peak -- sell then, whatever the P&L is at
    that point (could still be positive, could already be negative -- the
    trigger is the TREND, not a threshold).
  - STOP_LOSS_PCT: -4.0% -- Claude's own downside floor, same judgment basis
    as every other day-trade this account has run (SKHY 2026-07-10, NOC
    2026-07-14) -- the user accepted the risk ("亏钱不要来找我") but going in
    with zero floor while unsupervised (user asleep) is not what "maximize
    profit" requires. Never asked to be removed, so it stays.
  - Mandatory close-out ~15min before market close regardless of P&L -- never
    held past today, no exceptions -- backstop in case none of the above fire.

Cron: scoped to 2026-07-14 ONLY -- remove entries after today's close.
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

SYMBOLS = ['META', 'MU', 'SNDK', 'SKHY']
# SKHY added later same day: user explicitly overrode Claude's own decision to
# skip it ("168可以买" -- $168 is fine to buy) after it pulled back off its
# earlier intraday high ($172.79) toward the $168 level. Bought 18sh (whole
# shares only -- SKHY is not fractionable) @ $168.38, ~5% of equity. Same
# exit rules apply (trend-confirmed exit/-4% stop/mandatory close-out) --
# this is still today's day-trade, not the separate long-term skhy_position.py hold.
DOWNTREND_CONFIRM_TICKS = 2
STOP_LOSS_PCT = -4.0
STATE_FILE = '/home/qbao775/serenity-trader-stack/.bull_daytrade_20260714_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.bull_daytrade_20260714_done'


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


def manage_symbol(api, sym, state, mins_to_close, market_open):
    positions = [p for p in api.list_positions() if p.symbol == sym]
    if not positions:
        if state.get(sym, {}).get('closed'):
            return  # already handled
        state.setdefault(sym, {})['closed'] = True
        log(f"  {sym}: no position (already sold or never filled)")
        return

    p = positions[0]
    current_px = float(p.market_value) / float(p.qty)
    plpc = float(p.unrealized_plpc) * 100

    # Trend-confirmed exit off the peak. User: "我觉得只要从最高点开始下跌趋势
    # 就马上卖了" (sell as soon as a downtrend starts from the peak) + "不要等
    # 到亏钱再卖" (don't wait until it's losing money) -- track the peak P&L%
    # seen, and once DOWNTREND_CONFIRM_TICKS consecutive checks each come in
    # lower than the previous one (a confirmed reversal, not single-tick
    # noise), sell -- regardless of whether P&L is still positive.
    sym_state = state.setdefault(sym, {})
    peak = sym_state.get('peak_plpc', plpc)
    last_plpc = sym_state.get('last_plpc', plpc)
    decline_streak = sym_state.get('decline_streak', 0)

    if plpc > peak:
        peak = plpc
        decline_streak = 0
    elif plpc < last_plpc:
        decline_streak += 1
    else:
        decline_streak = 0

    sym_state['peak_plpc'] = peak
    sym_state['last_plpc'] = plpc
    sym_state['decline_streak'] = decline_streak
    downtrend_confirmed = decline_streak >= DOWNTREND_CONFIRM_TICKS

    log(f"  {sym}: qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}% peak={peak:+.2f}% "
        f"decline_streak={decline_streak}/{DOWNTREND_CONFIRM_TICKS}")

    reason = None
    if plpc <= STOP_LOSS_PCT:
        reason = f"亏损达到 {plpc:+.2f}% (<= {STOP_LOSS_PCT}%),止损离场"
    elif downtrend_confirmed:
        reason = f"从峰值 {peak:+.2f}% 开始连续{decline_streak}次走低 (现{plpc:+.2f}%),确认下跌趋势,立即卖出"
    elif market_open and mins_to_close <= 15:
        reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%,曾达到峰值{peak:+.2f}%),按规则强制平仓"

    if not reason:
        return

    o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD {sym} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
    sym_state['closed'] = True
    send_email(f"{'🎯' if plpc > 0 else '🛑'} {sym} 短线交易平仓",
               f"卖出价 ~${current_px:.2f}\n最终盈亏: {plpc:+.2f}%\n原因: {reason}")


def main():
    if os.path.exists(DONE_MARKER):
        log("all positions already closed out today — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60
    state = load_state()

    for sym in SYMBOLS:
        manage_symbol(api, sym, state, mins_to_close, clock.is_open)

    save_state(state)

    if all(state.get(sym, {}).get('closed') for sym in SYMBOLS):
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        log("all three positions closed out — marking done")


if __name__ == '__main__':
    main()
