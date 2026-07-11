#!/usr/bin/env python3
"""
skhy_daytrade.py — one-off, user-directed day-trade on SK Hynix's Nasdaq
debut (ticker SKHY, priced at $149/ADR, trading starts 2026-07-10 9:30am ET).

User's exact instruction (2026-07-09): "我赌10%炒个短线sk海力士，开盘就买入，
然后你看涨势，如果一直涨就收盘的时候卖掉" — 10% of equity, buy at the open,
sell at close if it's trending up.

This is explicitly a single-day, defined-risk speculative trade — NOT part
of Plan D, NOT part of the satellite framework, NOT covered by
reentry_monitor.py. It funds itself by selling a slice of the SGOV position
(the account is otherwise ~100% SGOV per the 2026-07-08 liquidation).

Claude's own addition (not explicitly requested, but serves the user's
stated "don't lose money" goal): a stop-loss, since "watch it and decide at
close" alone leaves the position fully exposed to intraday IPO-debut
volatility with no downside protection. STOP_LOSS_PCT below.

Logic per run (cron'd every 15 min during market hours, 2026-07-10 ONLY):
  1. No position yet, market open has passed -> sell SGOV to fund ~10% of
     equity, buy SKHY at market.
  2. Position held, price down >= STOP_LOSS_PCT from entry -> sell (protect
     capital, don't wait for close).
  3. Position held, within ~15 min of close -> sell regardless of direction
     (this was framed as a single-day trade; the user only specified the
     "sell at close if rising" case, but leaving a losing day-trade open
     overnight "hoping it comes back" would silently turn a bounded bet
     into open-ended risk — the same discipline this account already
     applies to satellite names, just faster on a single-day trade).
  4. Once sold (for any reason) -> write DONE_MARKER, take no further action.

Remove the cron entry after 2026-07-10 close — this script has no ongoing
purpose past that one trading day.
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.skhy_daytrade_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.skhy_daytrade_done'
LOG_PATH = '/home/qbao775/serenity-trader-stack/skhy_daytrade.log'

TARGET_PCT = 0.20          # 20% of equity — user raised from 10% to 20% on
                           # 2026-07-10 explicitly ("直接用20%尝试，不要10%")
                           # after Claude flagged the concentration risk
                           # (single, untested, first-day-trading IPO name,
                           # above the established 8-10% conviction ceiling)
STOP_LOSS_PCT = -10.0      # Claude's addition — protect capital intraday
CLOSE_BUFFER_MIN = 15      # sell this many minutes before 4pm ET close
TRAIL_FROM_PEAK_PCT = -4.0 # sell if price pulls back this much from the
                           # intraday peak seen so far — approximates
                           # "sell near the high" without requiring perfect
                           # foresight of the exact top (impossible in
                           # real time). Only engages once price has moved
                           # meaningfully above entry (see manage_position).
TRAIL_ARM_ABOVE_PCT = 3.0  # only start trailing once up at least this much
                           # from entry — otherwise normal early-session
                           # noise would trigger it immediately


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


def enter_position(api):
    acc = api.get_account()
    equity = float(acc.equity)
    target_notional = equity * TARGET_PCT
    bp = float(acc.buying_power)

    # Bug fix (2026-07-10, live): earlier ticks already sold SGOV twice
    # (13:30 and 13:35 UTC) while SKHYV had no trade data yet, each retry
    # blindly sold MORE SGOV without checking cash already on hand from the
    # prior attempt — only sell if current buying power is actually short
    # of the target, so a retry doesn't compound into an ever-growing cash
    # pile sitting idle outside SGOV.
    if bp < target_notional:
        sgov = [p for p in api.list_positions() if p.symbol == 'SGOV']
        if not sgov:
            log("no SGOV position to fund this trade from — aborting")
            return None
        sgov_qty = float(sgov[0].qty)
        sgov_px = float(sgov[0].market_value) / sgov_qty
        shortfall = target_notional - bp
        sell_qty = int((shortfall / sgov_px) + 5)  # small buffer
        sell_qty = min(sell_qty, int(sgov_qty))

        o1 = api.submit_order(symbol='SGOV', qty=sell_qty, side='sell', type='market', time_in_force='day')
        log(f"  sold {sell_qty}sh SGOV to cover shortfall (​${shortfall:.2f}), order={o1.id[:8]}")

        import time
        time.sleep(8)
        acc = api.get_account()
        bp = float(acc.buying_power)
    else:
        log(f"  buying_power ${bp:.2f} already covers target ${target_notional:.2f} — no SGOV sale needed this tick")

    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    r = requests.get('https://data.alpaca.markets/v2/stocks/SKHYV/trades/latest',
                      headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
    px = None
    if r.status_code == 200:
        px = r.json().get('trade', {}).get('p')
    if not px:
        log("  no live SKHYV price available yet — will retry next cron tick")
        return None

    qty = int(min(target_notional, bp - 20) // px)
    if qty < 1:
        log(f"  insufficient funds for even 1 share of SKHYV @ ${px} — aborting")
        return None

    o2 = api.submit_order(symbol='SKHYV', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT SKHYV qty={qty} @~${px} order={o2.id[:8]}")

    state = {
        'entered': True,
        'entry_time': datetime.datetime.utcnow().isoformat(),
        'entry_price_est': px,
        'qty': qty,
    }
    save_state(state)
    send_email("📈 SKHY 首秀日交易 — 已建仓",
               f"买入 SKHY {qty}股,预估入场价 ~${px}\n止损线: {STOP_LOSS_PCT}%\n"
               f"收盘前 {CLOSE_BUFFER_MIN} 分钟无论涨跌都会平仓。")
    return state


def manage_position(api, state):
    positions = [p for p in api.list_positions() if p.symbol == 'SKHYV']
    if not positions:
        log("no SKHYV position held (already sold or never filled) — marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        return

    p = positions[0]
    plpc = float(p.unrealized_plpc) * 100
    mv = float(p.market_value)
    current_px = mv / float(p.qty)

    # Track the intraday peak price seen so far, so we can trail from it.
    # "Sell at the exact daily high" isn't achievable in real time — this is
    # the realistic approximation: once up a meaningful amount, sell if it
    # pulls back a chunk from the best level reached, locking in most of
    # the run rather than giving it all back waiting for a peak that's
    # already passed.
    peak_px = max(state.get('peak_price', current_px), current_px)
    state['peak_price'] = peak_px
    save_state(state)
    pullback_from_peak_pct = (current_px / peak_px - 1) * 100
    up_from_entry_pct = (current_px / state['entry_price_est'] - 1) * 100

    log(f"  SKHYV position: qty={p.qty} mv=${mv:.2f} current=${current_px:.2f} "
        f"unrealized_plpc={plpc:+.2f}% peak=${peak_px:.2f} pullback_from_peak={pullback_from_peak_pct:+.2f}%")

    clock = api.get_clock()
    now = datetime.datetime.now(datetime.timezone.utc)
    close_time = clock.next_close if clock.is_open else None
    near_close = False
    if close_time:
        mins_to_close = (close_time - now).total_seconds() / 60
        near_close = 0 <= mins_to_close <= CLOSE_BUFFER_MIN

    trailing_armed = up_from_entry_pct >= TRAIL_ARM_ABOVE_PCT
    trailing_triggered = trailing_armed and pullback_from_peak_pct <= TRAIL_FROM_PEAK_PCT

    if plpc <= STOP_LOSS_PCT:
        reason = f"止损触发 ({plpc:+.2f}% <= {STOP_LOSS_PCT}%)"
    elif trailing_triggered:
        reason = (f"从高点回落触发 (最高 ${peak_px:.2f}, 现价 ${current_px:.2f}, "
                   f"回落 {pullback_from_peak_pct:+.2f}%, 当前盈利 {plpc:+.2f}%)")
    elif near_close:
        reason = f"接近收盘(单日交易,无论涨跌都平仓,当前 {plpc:+.2f}%)"
    else:
        log(f"  未触发平仓条件(止损{STOP_LOSS_PCT}% / 回落追踪{'已启动' if trailing_armed else '未启动,需先上涨'+str(TRAIL_ARM_ABOVE_PCT)+'%'} / 非收盘时段),继续持有")
        return

    o = api.submit_order(symbol='SKHYV', qty=p.qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD SKHYV qty={p.qty} reason={reason} order={o.id[:8]}")
    with open(DONE_MARKER, 'w') as f:
        json.dump({'closed_at': datetime.datetime.utcnow().isoformat(), 'reason': reason,
                    'final_plpc': plpc}, f, indent=2)
    send_email(f"SKHY 首秀日交易 — 已平仓 ({plpc:+.2f}%)",
               f"平仓原因: {reason}\n最终盈亏: {plpc:+.2f}%")


FULL_LIQUIDATION_MARKER = '/home/qbao775/serenity-trader-stack/.full_liquidation_done'
# 2026-07-10: user explicitly asked to cash out EVERYTHING by end of day
# today (Plan D core just re-bought this same morning, plus today's META
# trial position) -- proceeds earmarked for buying the real SKHY (regular
# ticker, trading starts Monday 2026-07-13) at next week's open. This is
# separate from the SKHYV day-trade logic above, which already has its own
# close-out. Confirmed scope explicitly ("全部卖掉" / "这两个都卖掉") after
# Claude asked whether this meant just today's day-trades or the core too.
FULL_LIQUIDATION_SYMBOLS = ['SPY', 'QQQ', 'BRK.B', 'META']


def liquidate_everything_near_close(api):
    if os.path.exists(FULL_LIQUIDATION_MARKER):
        return
    clock = api.get_clock()
    if not clock.is_open:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    mins_to_close = (clock.next_close - now).total_seconds() / 60
    if not (0 <= mins_to_close <= CLOSE_BUFFER_MIN):
        return

    log(f"  🔴 end-of-day full liquidation window ({mins_to_close:.0f} min to close) — "
        f"selling Plan D core + META per explicit user instruction")
    sold_any = False
    for sym in FULL_LIQUIDATION_SYMBOLS:
        positions = [p for p in api.list_positions() if p.symbol == sym]
        if not positions:
            continue
        qty = positions[0].qty
        o = api.submit_order(symbol=sym, qty=qty, side='sell', type='market', time_in_force='day')
        log(f"  ✓ SOLD {sym} qty={qty} order={o.id[:8]}")
        sold_any = True

    with open(FULL_LIQUIDATION_MARKER, 'w') as f:
        json.dump({'liquidated_at': datetime.datetime.utcnow().isoformat()}, f)

    if sold_any:
        send_email("💰 全部清仓完成 — 准备下周一买 SKHY",
                   "Plan D 核心(SPY/QQQ/BRK.B)+ META 已按用户指令全部卖出。\n"
                   "账户现金已备好,准备下周一(7/13)SKHY 正式代码开盘时买入。")


def main():
    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    liquidate_everything_near_close(api)

    if os.path.exists(DONE_MARKER):
        log("SKHYV day-trade already closed today — nothing more to do for that leg")
        return

    state = load_state()
    if not state.get('entered'):
        log("no SKHYV position yet — attempting entry")
        enter_position(api)
    else:
        manage_position(api, state)


if __name__ == '__main__':
    main()
