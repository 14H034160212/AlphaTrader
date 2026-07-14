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
  - Downtrend confirmation is ADAPTIVE, not a fixed tick count -- user:
    "势头好可以不设上限，势头一般可以自动调节" (if momentum is strong, no cap
    needed; if only moderate, auto-adjust). Peak P&L% >= STRONG_MOMENTUM_
    PEAK_PCT (3.0%) gets 3 consecutive declining checks before confirming a
    reversal (more patience for a real trend); a more moderate peak gets 2
    (less profit cushion to protect, confirm faster). Either way, once
    confirmed, sell regardless of whether P&L is still positive or already
    negative -- the trigger is the TREND, not a threshold.
  - STOP_LOSS_PCT: -4.0% -- Claude's own downside floor, same judgment basis
    as every other day-trade this account has run (SKHY 2026-07-10, NOC
    2026-07-14) -- the user accepted the risk ("亏钱不要来找我") but going in
    with zero floor while unsupervised (user asleep) is not what "maximize
    profit" requires. Never asked to be removed, so it stays.
  - Mandatory close-out ~15min before market close regardless of P&L -- never
    held past today, no exceptions -- backstop in case none of the above fire.
  - NOTE on "最终保证赚钱" (make sure it ends up profitable): no automated
    system can literally guarantee a profit -- real market risk exists on
    every trade. What this DOES do is bias every exit decision toward
    protecting whatever gain has already been established (adaptive trend-
    exit + hard stop-loss) rather than let a winner round-trip into a loser;
    that's the closest honest interpretation of the instruction.

User then escalated further: "一直关注一直买卖，一直赚钱" (keep watching
continuously, keep buying and selling continuously, keep making money
continuously) -- a single buy-hold-sell cycle per name isn't enough; once a
position closes (stop-loss, downtrend exit, whatever), watch that SAME name
for a fresh dip-and-recovery and re-enter, repeating for the rest of the
day. Deliberately bounded to these same 4 already-analyzed names rather than
scanning arbitrary new tickers (keeps every re-entry within names that
already have a real thesis behind them, not blind momentum-chasing across
the whole market) -- REENTRY logic below. Re-entry uses the same
recovery-off-a-fresh-low pattern already established in
skhy_position.py/mu_reentry.py/meta_longhold.py's original entry logic, just
tighter (1.0% vs those scripts' 1.5%, since this is an intraday re-dip, not
a multi-day pullback). No new entries within REENTRY_CUTOFF_MINS of close --
only exits are allowed that late, so nothing is left to unwind at the
mandatory close-out.

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
# DOWNTREND_CONFIRM_TICKS is now ADAPTIVE, not fixed -- user: "势头好可以不设
# 上限，势头一般可以自动调节" (if momentum is strong, no cap needed; if
# momentum is only moderate, auto-adjust). A strong peak (a real, convincing
# move) gets more patience before confirming a reversal -- don't get shaken
# out of a genuine trend by normal noise. A moderate/weak peak gets a
# tighter trigger -- there's less profit cushion to protect, so confirm
# faster and lock it in rather than risk giving it all back. See
# _confirm_ticks_for_peak() below for the exact thresholds.
STRONG_MOMENTUM_PEAK_PCT = 3.0
STOP_LOSS_PCT = -4.0
WEIGHTS = {'META': 0.08, 'MU': 0.05, 'SNDK': 0.04, 'SKHY': 0.05}
ENTRY_RECOVERY_FROM_LOW_PCT = 1.0   # tighter than the long-term scripts' 1.5% --
                                     # this is an intraday re-dip, not a multi-day pullback
REENTRY_CUTOFF_MINS = 20            # no NEW entries once this close to market close
# 2026-07-14: "sgov不要全部卖，就拿出来20%" -- total capital across all 4
# names combined is capped at 20% of equity; a new re-entry buy is only
# allowed if there's room left under this cap.
TOTAL_DEPLOY_CAP_PCT = 0.20
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


def manage_open_position(api, sym, p, state, mins_to_close, market_open):
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
    confirm_ticks = 3 if peak >= STRONG_MOMENTUM_PEAK_PCT else 2
    downtrend_confirmed = decline_streak >= confirm_ticks  # tracked/logged only, no longer acted on -- see below

    log(f"  {sym}: HOLDING qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}% peak={peak:+.2f}% "
        f"decline_streak={decline_streak}/{confirm_ticks}{' (downtrend confirmed, but riding to close per instruction)' if downtrend_confirmed else ''}")

    # 2026-07-14 (later same session): user said "不要管涨跌了" (stop worrying
    # about ups and downs) right after "今天势头都不错，你就买入吧，然后收盘
    # 的时候全部卖出" (momentum looks good today, just buy in, sell everything
    # at close) -- this REMOVES the downtrend-confirmed early exit as an
    # ACTIVE trigger (still tracked/logged above for visibility). Only the
    # stop-loss floor and the mandatory close-out can end a position now --
    # ride out normal intraday noise, no more selling on every reversal.
    reason = None
    if plpc <= STOP_LOSS_PCT:
        reason = f"亏损达到 {plpc:+.2f}% (<= {STOP_LOSS_PCT}%),止损离场"
    elif market_open and mins_to_close <= 15:
        reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%,曾达到峰值{peak:+.2f}%),按规则强制平仓"

    if not reason:
        return

    o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD {sym} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
    # reset per-position tracking so a future re-entry starts fresh, and drop
    # into "watching for the next dip" rather than marking this symbol done
    # for the day -- user: "一直关注一直买卖，一直赚钱"
    state[sym] = {'watch': None}
    send_email(f"{'🎯' if plpc > 0 else '🛑'} {sym} 短线交易平仓 (继续观察,寻找下次机会)",
               f"卖出价 ~${current_px:.2f}\n本轮盈亏: {plpc:+.2f}%\n原因: {reason}")


def manage_watching(api, sym, state, mins_to_close):
    if mins_to_close <= REENTRY_CUTOFF_MINS:
        return  # too close to end of day to open anything new

    import market_data as md
    q = md.get_stock_quote(sym)
    px = q['current'] if q and q.get('current') else None
    if not px:
        return

    sym_state = state.setdefault(sym, {})
    watch = sym_state.get('watch')
    if not watch:
        sym_state['watch'] = {'lowest_price': px}
        log(f"  {sym}: FLAT, watching for next dip from ${px:.2f}")
        return

    watch['lowest_price'] = min(watch['lowest_price'], px)
    recovery_pct = (px / watch['lowest_price'] - 1) * 100
    log(f"  {sym}: FLAT, watching — current=${px:.2f} low_seen=${watch['lowest_price']:.2f} "
        f"recovery={recovery_pct:+.2f}% (need {ENTRY_RECOVERY_FROM_LOW_PCT}%)")
    sym_state['watch'] = watch

    if recovery_pct < ENTRY_RECOVERY_FROM_LOW_PCT:
        return

    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)

    # 2026-07-14: user said "sgov不要全部卖，就拿出来20%" (don't sell all the
    # SGOV, only take out 20%) -- cap TOTAL capital deployed across these 4
    # names (not per-name) at 20% of equity. Already-overshot slightly at the
    # moment this was said (~$15,980 out vs a ~$12,187 cap); not unwound
    # retroactively, but no further room is allowed to make it worse.
    already_deployed = sum(float(p.market_value) for p in api.list_positions() if p.symbol in SYMBOLS)
    room = (equity * TOTAL_DEPLOY_CAP_PCT) - already_deployed
    if room <= 0:
        log(f"  {sym}: recovered, but total deployment (${already_deployed:.2f}) already at/over the "
            f"{TOTAL_DEPLOY_CAP_PCT*100:.0f}% cap — skipping this round")
        return

    notional = min(equity * WEIGHTS[sym], bp - 20, room)
    if notional < px:
        log(f"  {sym}: recovered but insufficient buying power/room — skipping this round")
        return

    fractionable = sym != 'SKHY'  # SKHY confirmed not fractionable earlier today
    qty = round(notional / px, 4) if fractionable else int(notional / px)
    if qty <= 0:
        return

    o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ RE-BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]} (recovered {recovery_pct:+.2f}% off low)")
    sym_state['watch'] = None
    send_email(f"📈 {sym} 再次建仓",
               f"从低点${watch['lowest_price']:.2f}回升{recovery_pct:+.2f}%,再次买入 {qty}股 @~${px:.2f}\n"
               f"继续沿用同样的止损/趋势退出规则。")


def main():
    if os.path.exists(DONE_MARKER):
        log("today's trading window is done — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60
    state = load_state()
    held_positions = {p.symbol: p for p in api.list_positions() if p.symbol in SYMBOLS}

    for sym in SYMBOLS:
        if sym in held_positions:
            manage_open_position(api, sym, held_positions[sym], state, mins_to_close, clock.is_open)
        else:
            manage_watching(api, sym, state, mins_to_close)

    save_state(state)

    # Done for the day once we're past the point new entries are allowed AND
    # nothing is currently held -- everything has been wound down for good.
    if mins_to_close <= REENTRY_CUTOFF_MINS and not held_positions:
        # 2026-07-14: "收盘的时候全部卖出然后买美债" (sell everything at close,
        # then buy US treasuries) -- once everything is confirmed flat, park
        # the freed cash back in SGOV rather than leaving it idle, same
        # instrument used every other time today. Runs once (guarded by
        # DONE_MARKER not existing yet).
        acc = api.get_account()
        bp = float(acc.buying_power)
        if bp > 50:
            import requests
            from database import SessionLocal, get_setting
            db = SessionLocal()
            k = get_setting(db, 'alpaca_api_key', 1); s = get_setting(db, 'alpaca_secret_key', 1)
            db.close()
            r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/trades/latest',
                              headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
            sgov_px = r.json().get('trade', {}).get('p')
            if sgov_px:
                qty = int((bp - 20) // sgov_px)
                if qty > 0:
                    # Bug fix (caught live, 2026-07-14): Alpaca rejects
                    # extended_hours=True combined with type='market'
                    # ("extended hours order must be DAY or GTC limit
                    # orders") -- this code only runs while main() has
                    # already confirmed clock.is_open, i.e. regular market
                    # hours, so extended_hours was unnecessary and wrong here.
                    o = api.submit_order(symbol='SGOV', qty=qty, side='buy', type='market',
                                          time_in_force='day')
                    log(f"  ✓ parked ${qty*sgov_px:.2f} back into SGOV ({qty}sh @ ~${sgov_px:.2f}) order={o.id[:8]}")
                    send_email("💵 今日交易结束 — 资金已转回美债(SGOV)",
                               f"今天所有短线仓位已清空,剩余资金 {qty}股 SGOV @~${sgov_px:.2f} 已买入停放。")

        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        log("past the re-entry cutoff with nothing held — marking today's trading done")


if __name__ == '__main__':
    main()
