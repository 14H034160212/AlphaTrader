#!/usr/bin/env python3
"""
momentum_chase_20260715.py — ONE-OFF, 2026-07-15 ONLY. User: "开盘以后可以关
注一下哪只股票涨势比较好，然后可以追涨" (after open, watch which stock has
the best momentum, then chase it) -- asked to scope this to the whole
market ("完全开放，全市场扫"), explicitly reversing the account's standing
"don't chase" discipline for today only.

Since today's capital is already 100% deployed into SPY/QQQ
(plan_d_daytrade_20260715.py), "chasing" here means: scan the whole market
for a genuine momentum standout, and if one clearly emerges, TRIM SPY/QQQ
proportionally and reallocate into it -- directly applying the "dynamic
reallocation toward the winner" lesson from yesterday's retrospective
(feedback_trading_execution_autonomy.md) rather than adding fresh leverage.

Universe: Yahoo Finance's built-in "day_gainers" screener (yf.screen), the
whole market, not limited to any preset watchlist. Basic quality filters
(NOT a fundamentals check -- this is explicitly a momentum chase, not a
thesis-driven buy):
  - price >= $10 (avoid penny/microcap manipulation risk)
  - today's volume so far >= 300,000 shares (liquid enough to trade cleanly)
  - move >= MIN_MOMENTUM_PCT (8%) -- a genuine standout, not noise
  - must be actually tradeable on Alpaca (checked live, not assumed)
  - not already held in this account

Chases ONE name at a time, but ROTATES continuously per later same-day
instructions: "回落的话你要同时看有没有其他涨势更好的，可以接着买涨势更好的"
(if it pulls back, also check whether something else has better momentum,
and chase that instead) -- when the current chase gets stopped out, don't
stop for the day, immediately look for the next-best qualifying candidate
and chase that instead, repeating until the pre-close cutoff. A stopped-out
symbol is excluded from re-consideration for the rest of the day (avoid
whipsaw-looping into the same name that already proved unstable).
Reallocates CHASE_ALLOCATION_PCT (15%) of equity, trimmed proportionally
from the current SPY/QQQ position value, each time it rotates.

MIN_MOMENTUM_PCT is TIME-ADAPTIVE per "开盘前30-60分钟要不要降低确认门槛
(4-5%就追)" -- confirmed: lower to 4-5% in the first hour of trading (catch
a move earlier, in exchange for a higher false-positive rate), then back to
the stricter 8% for the rest of the day (require a genuinely confirmed
standout once the easy early-session edge is gone).

Exit rules -- REVISED before this had a chance to fire. User: "不要加止损，
如果下跌就一开始都不要买，买入就要在你判断上升的时候买" (don't add a
stop-loss; if declining don't buy in the first place; only buy when you
judge it's rising). No fixed stop-loss percentage: exit on a
DOWNTREND_CONFIRM_TICKS-consecutive-decline trend reversal off the peak
(same mechanism as plan_d_daytrade_20260715.py's revised exit), which is
also what triggers the rotation into the next candidate. Mandatory
close-out ~15min before market close remains the hard backstop regardless.
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

MIN_PRICE = 10.0
MIN_VOLUME = 300_000
MIN_MOMENTUM_PCT_EARLY = 4.5   # first EARLY_WINDOW_MIN of trading -- catch it sooner
MIN_MOMENTUM_PCT_LATER = 8.0   # rest of the day -- require a real confirmed standout
EARLY_WINDOW_MIN = 60
CHASE_ALLOCATION_PCT = 0.15
DOWNTREND_CONFIRM_TICKS = 2   # consecutive declining checks off the peak -- no fixed stop-loss ("不要加止损")
SAFETY_MARGIN_PCT = 1.0   # below this plpc, exit on the FIRST decline tick instead of waiting ("确保...都是赚钱的卖的")
TRIM_FROM = ['SPY', 'QQQ']
STATE_FILE = '/home/qbao775/serenity-trader-stack/.momentum_chase_20260715_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.momentum_chase_20260715_done'


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


def find_candidate(api, already_held, excluded, min_momentum_pct):
    import yfinance as yf
    try:
        res = yf.screen('day_gainers', count=25)
    except Exception as e:
        log(f"  screener fetch failed: {e}")
        return None

    for q in res.get('quotes', []):
        sym = q.get('symbol')
        px = q.get('regularMarketPrice')
        chg = q.get('regularMarketChangePercent')
        vol = q.get('regularMarketVolume')
        if not sym or not px or not chg or not vol:
            continue
        if sym in already_held or sym in TRIM_FROM or sym in excluded:
            continue
        if px < MIN_PRICE or vol < MIN_VOLUME or chg < min_momentum_pct:
            continue
        try:
            asset = api.get_asset(sym)
            if not (asset.tradable and asset.status == 'active'):
                continue
        except Exception:
            continue
        return {'symbol': sym, 'price': px, 'change_pct': chg, 'volume': vol}
    return None


def enter_chase(api, state, mins_since_open):
    held_symbols = {p.symbol for p in api.list_positions()}
    excluded = set(state.get('stopped_out_symbols', []))
    min_momentum_pct = MIN_MOMENTUM_PCT_EARLY if mins_since_open <= EARLY_WINDOW_MIN else MIN_MOMENTUM_PCT_LATER
    candidate = find_candidate(api, held_symbols, excluded, min_momentum_pct)
    if not candidate:
        log(f"  no qualifying momentum candidate found this tick (threshold={min_momentum_pct}%)")
        return

    sym = candidate['symbol']
    log(f"  candidate found: {sym} +{candidate['change_pct']:.1f}% px=${candidate['price']:.2f} vol={candidate['volume']:,}")

    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * CHASE_ALLOCATION_PCT
    # Bug fix (caught before market open, 2026-07-15): only trim SPY/QQQ for
    # the SHORTFALL beyond cash already sitting free (e.g. proceeds from a
    # previous chase's exit) -- the original version trimmed a FULL 15% out
    # of SPY/QQQ on every rotation regardless of existing cash, which over
    # many stop-out-and-rotate cycles in a single unsupervised day would
    # progressively over-deplete SPY/QQQ while leaving prior proceeds sitting
    # idle uninvested instead of being recycled.
    shortfall = target_notional - bp

    if shortfall > 0:
        trim_positions = [p for p in api.list_positions() if p.symbol in TRIM_FROM]
        total_trimmable = sum(float(p.market_value) for p in trim_positions)
        if total_trimmable < shortfall:
            log(f"  not enough in SPY/QQQ to trim (${total_trimmable:.2f} < shortfall ${shortfall:.2f}) — skipping chase")
            return
        for p in trim_positions:
            trim_notional = shortfall * (float(p.market_value) / total_trimmable)
            trim_qty = round(trim_notional / (float(p.market_value) / float(p.qty)), 4)
            if trim_qty <= 0:
                continue
            o = api.submit_order(symbol=p.symbol, qty=trim_qty, side='sell', type='market', time_in_force='day')
            log(f"  trimmed {p.symbol} qty={trim_qty} order={o.id[:8]} to fund the chase (shortfall ${shortfall:.2f})")
        import time
        time.sleep(8)
    else:
        log(f"  enough free cash (${bp:.2f}) already to fund this chase — no SPY/QQQ trim needed")

    acc = api.get_account()
    bp = float(acc.buying_power)
    notional = min(target_notional, bp - 20)
    qty = round(notional / candidate['price'], 4)
    if qty <= 0:
        log("  insufficient buying power after trim — aborting chase")
        return

    o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ CHASED {sym} qty={qty} @~${candidate['price']:.2f} order={o.id[:8]}")
    state['chased_symbol'] = sym
    state['entered'] = True
    # reset trend-tracking for the new position -- don't inherit the
    # previous chase's peak/decline history
    state['chase_peak_plpc'] = 0.0
    state['chase_last_plpc'] = 0.0
    state['chase_decline_streak'] = 0
    save_state(state)
    send_email(f"🚀 追涨建仓: {sym}",
               f"从SPY/QQQ抽出约{CHASE_ALLOCATION_PCT*100:.0f}%资金,追入当天全市场涨幅最强之一的 {sym}"
               f"(当时涨幅+{candidate['change_pct']:.1f}%)\n"
               f"买入 {qty}股 @~${candidate['price']:.2f}\n"
               f"不设固定止损——判断转跌趋势就离场(并找下一个更强的机会),"
               f"收盘前15分钟无论如何强制平仓,不会留到明天。")


def manage_chase(api, state, mins_to_close):
    """Returns True if the chase slot is now free (position closed and NOT
    at the final cutoff) -- caller should immediately look for a fresh
    candidate rather than waiting for the next cron tick."""
    sym = state.get('chased_symbol')
    if not sym:
        return False
    positions = [p for p in api.list_positions() if p.symbol == sym]
    if not positions:
        return False
    p = positions[0]
    current_px = float(p.market_value) / float(p.qty)
    plpc = float(p.unrealized_plpc) * 100

    # Trend-confirmed exit, no fixed stop-loss ("不要加止损") -- track the
    # peak P&L% and exit once DOWNTREND_CONFIRM_TICKS consecutive checks
    # each come in lower than the previous one. This is ALSO what triggers
    # rotating into the next candidate per "回落的话你要同时看有没有其他涨势
    # 更好的".
    peak = state.get('chase_peak_plpc', plpc)
    last_plpc = state.get('chase_last_plpc', plpc)
    decline_streak = state.get('chase_decline_streak', 0)
    if plpc > peak:
        peak = plpc
        decline_streak = 0
    elif plpc < last_plpc:
        decline_streak += 1
    else:
        decline_streak = 0
    state['chase_peak_plpc'] = peak
    state['chase_last_plpc'] = plpc
    state['chase_decline_streak'] = decline_streak
    # 2026-07-15: "确保每只股票你卖的时候都是赚钱的卖的" -- near breakeven,
    # exit on the first decline tick instead of waiting for confirmation.
    required_ticks = 1 if plpc < SAFETY_MARGIN_PCT else DOWNTREND_CONFIRM_TICKS
    downtrend_confirmed = decline_streak >= required_ticks

    log(f"  {sym} (chased): qty={p.qty} px=${current_px:.2f} plpc={plpc:+.2f}% peak={peak:+.2f}% "
        f"decline_streak={decline_streak}/{required_ticks}")

    reason = None
    hit_close_cutoff = mins_to_close <= 15
    if downtrend_confirmed:
        reason = f"从峰值 {peak:+.2f}% 开始连续{decline_streak}次走低 (现{plpc:+.2f}%),判断转跌离场(无固定止损)"
    elif hit_close_cutoff:
        reason = f"距收盘不到15分钟 (盈亏 {plpc:+.2f}%),按规则强制平仓"

    if not reason:
        save_state(state)
        return False

    o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD {sym} qty={p.qty} @~${current_px:.2f} order={o.id[:8]} — {reason}")
    send_email(f"{'🎯' if plpc > 0 else '🛑'} {sym} 追涨仓位平仓" +
               ("" if hit_close_cutoff else " (继续寻找下一个更强的机会)"),
               f"卖出价 ~${current_px:.2f}\n最终盈亏: {plpc:+.2f}%\n原因: {reason}")

    # 2026-07-15: user said "回落的话你要同时看有没有其他涨势更好的，可以接着
    # 买涨势更好的" (if it pulls back, check for a better-momentum name and
    # chase that instead) -- rotate rather than stop for the day, unless
    # this exit was the final mandatory close-out.
    stopped_out = state.setdefault('stopped_out_symbols', [])
    if sym not in stopped_out:
        stopped_out.append(sym)
    state['chased_symbol'] = None
    state['entered'] = False
    save_state(state)

    if hit_close_cutoff:
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat(), 'symbol': sym, 'final_plpc': plpc}, f)
        return False
    return True


def main():
    if os.path.exists(DONE_MARKER):
        log("today's momentum chase already resolved — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    now = datetime.datetime.now(clock.next_close.tzinfo)
    mins_to_close = (clock.next_close - now).total_seconds() / 60
    market_open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_since_open = max(0, (now - market_open_today).total_seconds() / 60)
    state = load_state()

    if state.get('entered'):
        rotated_free = manage_chase(api, state, mins_to_close)
        if not rotated_free:
            return
        state = load_state()  # manage_chase already saved the cleared state

    if mins_to_close <= 30:
        log("  too close to end of day to start a fresh chase — skipping for the rest of today")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'skipped': True, 'reason': 'no qualifying entry before cutoff'}, f)
        return
    enter_chase(api, state, mins_since_open)


if __name__ == '__main__':
    main()
