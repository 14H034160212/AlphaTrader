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

Rules -- REVISED before any entry fired (market hadn't opened yet). User:
"不要加止损，如果下跌就一开始都不要买，买入就要在你判断上升的时候买" (don't
add a stop-loss; if it's declining don't buy in the first place; only buy
when you judge it's rising). This replaces both ends with TREND JUDGMENT
instead of fixed thresholds:
  - ENTRY: wait for a confirmed short-term uptrend before buying -- track
    the price since first observed; only buy once it has risen for
    ENTRY_CONFIRM_TICKS consecutive checks (a real move, not a single-tick
    blip). Do NOT buy while it's still flat/declining from where it was
    first observed.
  - EXIT: no fixed stop-loss percentage. Track the peak P&L% and exit once
    DOWNTREND_CONFIRM_TICKS consecutive checks each come in lower than the
    previous one -- a confirmed reversal off the peak, same trend-judgment
    principle applied symmetrically to selling.
  - Mandatory close-out ~15min before market close regardless of P&L --
    still the hard backstop no matter what the trend logic says.
  - No re-entry cycle (unlike bull_day_trade_20260714.py) -- this is a
    single overnight-risk-driven trade, not meant to be repeatedly rebought
    all day; once exited (by trend-reversal or close), stays flat.

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
ENTRY_CONFIRM_TICKS = 1   # 2026-07-15: user cited 葛兰比法则(Granville's Rules) --
# buy on the FIRST bullish tick; 2+ consecutive big up-moves raise pullback
# risk, so waiting for a second confirmation was buying too late/too extended.
DOWNTREND_CONFIRM_TICKS = 2   # unused now (see manage()) -- kept only so old state files still parse
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


def record_action(state, text):
    # 2026-07-15: "你不要每次操作就发一封邮件你可以等收盘的时候把今天的战况
    # 整理一起发" (don't email on every single trade, batch a summary at
    # close) -- accumulate here instead of emailing per-trade.
    log_entry = f"[{datetime.datetime.utcnow().strftime('%H:%M UTC')}] {text}"
    state.setdefault('action_log', []).append(log_entry)
    save_state(state)


def send_daily_summary(state):
    actions = state.get('action_log', [])
    if not actions:
        return
    body = "今天(Plan D名称短线交易, SPY/QQQ)战况汇总:\n\n" + "\n".join(actions)
    send_email(f"📊 Plan D 短线交易 - 今日汇总 ({datetime.datetime.utcnow():%Y-%m-%d})", body)


def enter(api, state):
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)

    for sym, w in WEIGHTS.items():
        sym_state = state.setdefault(sym, {})
        if sym_state.get('entered'):
            continue

        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            log(f"  {sym}: no live price yet — will retry next tick")
            continue

        # Trend-confirmed entry -- user: "如果下跌就一开始都不要买，买入就要在
        # 你判断上升的时候买". Track price since first observed; only buy once
        # it has risen for ENTRY_CONFIRM_TICKS consecutive checks.
        last_px = sym_state.get('last_px')
        rise_streak = sym_state.get('rise_streak', 0)
        if last_px is None:
            sym_state['last_px'] = px
            sym_state['rise_streak'] = 0
            save_state(state)
            log(f"  {sym}: first price observed ${px:.2f} — watching for a confirmed uptrend before buying")
            continue

        if px > last_px:
            rise_streak += 1
        else:
            rise_streak = 0
        sym_state['last_px'] = px
        sym_state['rise_streak'] = rise_streak

        if rise_streak < ENTRY_CONFIRM_TICKS:
            log(f"  {sym}: px=${px:.2f} rise_streak={rise_streak}/{ENTRY_CONFIRM_TICKS} — not confirmed yet, not buying")
            save_state(state)
            continue

        notional = min(equity * w, bp - 20)
        qty = round(notional / px, 4)
        if qty <= 0:
            log(f"  {sym}: insufficient buying power — skipping")
            continue
        o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
        log(f"  ✓ BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]} (confirmed uptrend, {rise_streak} consecutive rises)")
        state[sym] = {'entered': True}
        bp -= notional
        save_state(state)
        record_action(state, f"买入 {sym} {qty}股 @~${px:.2f} (确认{rise_streak}次连续上涨后进场)")


SPY_QQQ_REBALANCE_GAP_PCT = 1.5   # min plpc divergence before shifting capital between them
SPY_QQQ_REBALANCE_SHARE = 0.30    # move this fraction of the laggard's value to the leader per rebalance
REBALANCE_COOLDOWN_MIN = 10       # don't re-rebalance within this many minutes of the last one


def rebalance_spy_qqq(api, state, mins_to_close):
    # 2026-07-15: user said "包括如果你觉得qqq涨势更好，也可以卖掉spy买qqq，反之
    # 也是" (if QQQ's momentum looks better, sell SPY and buy QQQ, and vice
    # versa) -- dynamic reallocation BETWEEN the two names, same principle as
    # the momentum-chase script but staying within today's two picks. Only
    # acts on a real, sustained gap (not single-tick noise) and stays off
    # once too close to the mandatory close-out.
    if mins_to_close <= 20:
        return
    last_rebalance = state.get('_last_rebalance_mins_ago')
    now_iso = datetime.datetime.utcnow().isoformat()
    if last_rebalance:
        elapsed = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_rebalance)).total_seconds() / 60
        if elapsed < REBALANCE_COOLDOWN_MIN:
            return

    positions = {p.symbol: p for p in api.list_positions() if p.symbol in WEIGHTS}
    if len(positions) < 2:
        return  # need both legs present to compare/rebalance

    plpc = {sym: float(p.unrealized_plpc) * 100 for sym, p in positions.items()}
    leader = max(plpc, key=plpc.get)
    laggard = min(plpc, key=plpc.get)
    gap = plpc[leader] - plpc[laggard]
    if gap < SPY_QQQ_REBALANCE_GAP_PCT:
        return

    lag_p = positions[laggard]
    trim_notional = float(lag_p.market_value) * SPY_QQQ_REBALANCE_SHARE
    trim_qty = round(trim_notional / (float(lag_p.market_value) / float(lag_p.qty)), 4)
    if trim_qty <= 0:
        return
    o = api.submit_order(symbol=laggard, qty=trim_qty, side='sell', type='market', time_in_force='day')
    log(f"  ↔ rebalance: {laggard} ({plpc[laggard]:+.2f}%) lagging {leader} ({plpc[leader]:+.2f}%) "
        f"by {gap:.2f}pp — trimmed {trim_qty}sh order={o.id[:8]}")
    record_action(state, f"调仓: {laggard}({plpc[laggard]:+.2f}%)落后{leader}({plpc[leader]:+.2f}%){gap:.1f}个百分点,"
                          f"减仓{laggard} {trim_qty}股转投{leader}")

    import time
    time.sleep(6)
    acc = api.get_account()
    bp = float(acc.buying_power)
    import market_data as md
    q = md.get_stock_quote(leader)
    px = q['current'] if q and q.get('current') else None
    if px and bp > 20:
        add_qty = round((bp - 20) / px, 4)
        if add_qty > 0:
            o2 = api.submit_order(symbol=leader, qty=add_qty, side='buy', type='market', time_in_force='day')
            log(f"  ↔ rebalance: added {add_qty}sh {leader} @~${px:.2f} order={o2.id[:8]}")
    state['_last_rebalance_mins_ago'] = now_iso
    save_state(state)


def manage(api, state):
    positions = {p.symbol: p for p in api.list_positions() if p.symbol in WEIGHTS}
    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60

    # rebalance_spy_qqq() call REMOVED 2026-07-15 -- user: "不要这样频繁买卖了"
    # (stop trading this frequently). Hold to close now, no intraday
    # rebalancing between SPY/QQQ either.

    for sym in WEIGHTS:
        if sym not in positions:
            continue
        p = positions[sym]
        current_px = float(p.market_value) / float(p.qty)
        plpc = float(p.unrealized_plpc) * 100

        # Trend-confirmed exit, no fixed stop-loss -- user: "不要加止损"。
        # Track the peak P&L% seen and exit once DOWNTREND_CONFIRM_TICKS
        # consecutive checks each come in lower than the previous one.
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
        # 2026-07-15: REMOVED entirely (was causing real churn losses -- see
        # commit history). User: "没必要，你就选好今天利好的股票拿到收盘然后
        # 卖掉就可以" + "不要这样频繁买卖了" (no need for all that, just pick
        # good stocks and hold to close, then sell -- stop trading this
        # frequently). No more intraday exit logic at all now -- once bought,
        # hold unconditionally until the mandatory close-out. peak/decline
        # tracking kept for logging only.
        log(f"  {sym}: qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}% peak={peak:+.2f}% (holding to close)")

        reason = None
        if mins_to_close <= 15:
            reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%),按规则强制平仓"

        if not reason:
            continue

        o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
        log(f"  ✓ SOLD {sym} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
        state.setdefault(sym, {})['closed'] = True
        record_action(state, f"卖出 {sym} @~${current_px:.2f} 盈亏{plpc:+.2f}% — {reason}")

    save_state(state)

    still_held = {p.symbol for p in api.list_positions() if p.symbol in WEIGHTS}
    if mins_to_close <= 15 and not still_held and all(state.get(s, {}).get('entered') for s in WEIGHTS):
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        log("today's Plan-D-names day-trade fully wound down — marking done")
        send_daily_summary(state)


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
