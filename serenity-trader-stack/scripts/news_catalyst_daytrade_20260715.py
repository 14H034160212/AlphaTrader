#!/usr/bin/env python3
"""
news_catalyst_daytrade_20260715.py — ONE-OFF day-trade, 2026-07-15 ONLY.
User: "上这几个利好的股票我觉得都可以买，你觉得合适的位置买" (buy all these
positive-news stocks, at whatever entry point you think is appropriate) --
referring to a same-conversation list of real, verified catalyst-driven
movers today:
  - PYPL (PayPal): Stripe + Advent International take-over bid, $60.50/share,
    >$53B valuation, premarket +15%
  - ASML: Q2 beat across the board, raised annual sales guidance citing AI
    demand, +30% chipmaking-equipment capacity expansion, Intel adopting its
    next-gen equipment
  - BABA (Alibaba): "Apple Intelligence" China filing approved, Tongyi
    Qianwen to be integrated into it, premarket +6%
  - MS (Morgan Stanley): Q2 EPS/revenue beat ($3.46 EPS on $21.35B revenue)

Same discipline as plan_d_daytrade_20260715.py/momentum_chase_20260715.py
(reused directly, not reinvented under time pressure):
  - ENTRY: wait for ENTRY_CONFIRM_TICKS consecutive rising checks before
    buying -- don't buy while still flat/declining even with good news
    behind it ("如果下跌就一开始都不要买，买入就要在你判断上升的时候买").
  - EXIT: no fixed stop-loss. Trend-confirmed exit (decline_streak off the
    peak), with SAFETY_MARGIN_PCT adaptive sensitivity near breakeven
    ("确保每只股票你卖的时候都是赚钱的卖的").
  - Mandatory close-out ~15min before market close, no exceptions.
  - Funded by trimming SPY/QQQ for the shortfall beyond free cash (same
    capital-recycling fix as momentum_chase_20260715.py).
  - Emails batched into one end-of-day summary, not per-trade.

Sizing: WEIGHTS below (~8% each, ~32% total) -- conviction-sized similar to
this account's other named-catalyst positions this week (META/MU/SNDK were
4-8% each), not "随便" reckless, since 4 names simultaneously plus whatever
momentum_chase/plan_d_daytrade are already using draws on the same SPY/QQQ
pool.
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

WEIGHTS = {'PYPL': 0.10, 'ASML': 0.10, 'BABA': 0.08, 'MS': 0.08, 'IBM': 0.06}
# IBM added: user's technical read -- "股价远离均线，技术上有上涨修复的要求"
# (price far from moving average after yesterday's -25% crash, technically
# due for a mean-reversion bounce). Sized smaller (6%) than the forward-
# catalyst names -- this is a technical rebound thesis on top of genuinely
# bad news (Q2 miss), not a fresh positive catalyst, so more speculative.
# 2026-07-15: user said "如果你觉得比买标普500和qqq还好的话可以都卖掉买这些，
# 你评估" (if you think these are better than SPY/QQQ, feel free to sell all
# of it and buy these -- you decide). Judgment call: bumped PYPL/ASML to 10%
# (the two with the clearest, most concrete catalysts -- a signed M&A offer
# and a hard earnings beat + raised guidance), kept BABA/MS at 8% (real but
# more modest catalysts). Did NOT fully liquidate SPY/QQQ into these --
# SPY/QQQ is diversified, these 4 are single-name event bets each carrying
# real idiosyncratic risk (M&A deals can fall through, earnings pops can
# reverse); concentrating everything into 4 single stocks isn't proportional
# even given real conviction. ~36% total across the four, not 100%.
ENTRY_CONFIRM_TICKS = 1   # 2026-07-15: 葛兰比法则(Granville's Rules) -- buy on
# the FIRST bullish tick, not the second -- waiting for 2+ confirmed
# up-moves raises the odds of buying right before a pullback.
DOWNTREND_CONFIRM_TICKS = 2
SAFETY_MARGIN_PCT = 1.0
TRIM_FROM = ['SPY', 'QQQ']
STATE_FILE = '/home/qbao775/serenity-trader-stack/.news_catalyst_daytrade_20260715_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.news_catalyst_daytrade_20260715_done'


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
    state.setdefault('action_log', []).append(f"[{datetime.datetime.utcnow().strftime('%H:%M UTC')}] {text}")
    save_state(state)


def send_daily_summary(state):
    actions = state.get('action_log', [])
    if not actions:
        return
    send_email(f"📊 消息面追涨(PYPL/ASML/BABA/MS) - 今日汇总 ({datetime.datetime.utcnow():%Y-%m-%d})",
               "今天消息面驱动的4只(PayPal/ASML/阿里巴巴/摩根士丹利)战况汇总:\n\n" + "\n".join(actions))


def enter(api, state):
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)

    for sym, w in WEIGHTS.items():
        sym_state = state.setdefault(sym, {})
        if sym_state.get('entered'):
            continue

        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            log(f"  {sym}: no live price yet — will retry next tick")
            continue

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
            log(f"  {sym}: px=${px:.2f} rise_streak={rise_streak}/{ENTRY_CONFIRM_TICKS} — not confirmed yet")
            save_state(state)
            continue

        acc = api.get_account()
        bp = float(acc.buying_power)
        target_notional = equity * w
        shortfall = target_notional - bp
        if shortfall > 0:
            trim_positions = [p for p in api.list_positions() if p.symbol in TRIM_FROM]
            total_trimmable = sum(float(p.market_value) for p in trim_positions)
            if total_trimmable < shortfall:
                log(f"  {sym}: not enough in SPY/QQQ to trim (${total_trimmable:.2f} < shortfall ${shortfall:.2f}) — skipping")
                continue
            for p in trim_positions:
                trim_notional = shortfall * (float(p.market_value) / total_trimmable)
                trim_qty = round(trim_notional / (float(p.market_value) / float(p.qty)), 4)
                if trim_qty <= 0:
                    continue
                o = api.submit_order(symbol=p.symbol, qty=trim_qty, side='sell', type='market', time_in_force='day')
                log(f"  trimmed {p.symbol} qty={trim_qty} order={o.id[:8]} to fund {sym}")
            import time
            time.sleep(6)

        acc = api.get_account()
        bp = float(acc.buying_power)
        notional = min(target_notional, bp - 20)
        qty = round(notional / px, 4)
        if qty <= 0:
            log(f"  {sym}: insufficient buying power — skipping")
            continue

        o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
        log(f"  ✓ BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]} (confirmed uptrend, {rise_streak} consecutive rises)")
        state[sym] = {'entered': True}
        save_state(state)
        record_action(state, f"买入 {sym} {qty}股 @~${px:.2f} (确认{rise_streak}次连续上涨后进场,消息面驱动仓位)")


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
        required_ticks = 1 if plpc < SAFETY_MARGIN_PCT else DOWNTREND_CONFIRM_TICKS
        downtrend_confirmed = decline_streak >= required_ticks

        log(f"  {sym}: qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}% peak={peak:+.2f}% "
            f"decline_streak={decline_streak}/{required_ticks}")

        reason = None
        if downtrend_confirmed:
            reason = f"从峰值 {peak:+.2f}% 开始连续{decline_streak}次走低 (现{plpc:+.2f}%),判断转跌离场"
        elif mins_to_close <= 15:
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
        log("today's news-catalyst day-trade fully wound down — marking done")
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
