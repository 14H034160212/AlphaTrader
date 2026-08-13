#!/usr/bin/env python3
"""
skhy_position.py — longer-term SK Hynix (SKHY, regular ticker from
2026-07-13) position, per explicit user instruction (2026-07-11):

"接下来你可以等下周开始卖海力士，但是不要因为跌就卖掉，我觉得你可以拿住
然后等超过200在卖" — buy at Monday's open, do NOT sell on weakness, hold
regardless of drawdown, only sell once price >= $200.

Claude asked to confirm whether this meant literally no downside limit at
all vs. a wide catastrophe-only stop. User's answer: "不设任何下线" — no
downside limit whatsoever. This is DELIBERATELY different from
skhy_daytrade.py's tight stop-loss/trailing-stop/close-out discipline —
this is a genuine buy-and-hold position with a single take-profit target,
not a day-trade. No stop-loss means real, uncapped downside exposure is
accepted on purpose here; do not add one back in without the user asking.

Logic per run (cron'd periodically, NOT scoped to a single day — this
persists until the target is hit):
  1. No position yet, market open, ticker SKHY has live data -> buy with
     the allocated notional (~20% of equity, same sizing as the prior
     day-trade, recomputed against current equity at execution time).
  2. Position held, price >= TARGET_PRICE -> sell, done.
  3. Position held, price < TARGET_PRICE -> hold, no matter how far down.
     No stop-loss. No time limit. This is intentional.

Remove SATELLITE_BUYING_PAUSED-style marker only when sold or if the user
explicitly changes the plan.
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.skhy_position_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.skhy_position_done'
# 2026-08-13: the 2026-07-13 confirm-before-buy policy is REVERSED (user
# restored full autonomy across SKHY/MU/META/satellite/core-reentry, same
# scope 07-13 had narrowed). Caveat added in the same breath: "但是你要确保
# 很有信心才买" (must be confident before buying) and "不要到时候出现...买入
# 没有买入的情况" (don't let a buy-that-didn't-buy situation happen) --
# see the identical, already-tested pattern in mu_reentry.py. CONFIRM_FILE
# kept only as an unused manual-override path.
CONFIRM_FILE = '/home/qbao775/serenity-trader-stack/.ENTRY_CONFIRMED_SKHY'
CLAUDE_BIN = '/home/qbao775/.local/bin/claude'
CONFIDENCE_RECHECK_COOLDOWN_MIN = 60
SIM_STATE_FILE = '/home/qbao775/serenity-trader-stack/.skhy_position_sim.json'

TARGET_PCT = 0.20        # same 20% sizing as the day-trade, user hasn't said otherwise
TARGET_PRICE = 200.0     # sell ONLY when price >= this. No other exit condition.

# 2026-07-11: user asked not to buy mechanically at the open — watch price
# action first; if it's declining, wait for a sign of stabilization rather
# than buying into a falling knife (this is a reaction to Friday's SKHYV
# entry being poorly timed near a local high). "A good entry point" can't
# be fully quantified, so this is a reasonable approximation, not a
# guarantee: track the lowest price seen since we started observing, buy
# once price has recovered a bit off that low -- or once a max wait has
# elapsed, so we don't end up waiting forever and missing the entry
# entirely if it never clearly stabilizes.
ENTRY_RECOVERY_FROM_LOW_PCT = 1.5   # buy once price is up this much from the observed low
ENTRY_MAX_WAIT_MIN = 120             # give up waiting and just buy after this long


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


def get_live_price(api, symbol):
    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    r = requests.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest',
                      headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
    if r.status_code == 200:
        return r.json().get('trade', {}).get('p')
    return None


def _confidence_check(sym, current_price, reason):
    """Same pattern as mu_reentry.py (tested there 2026-08-13): a fresh,
    research-backed judgment call replacing the removed human-confirmation
    gate. The mechanical entry condition is a chase-guard, not the full
    judgment -- defaults to WAIT on any ambiguity or call failure."""
    import subprocess
    prompt = (
        f"你是一个长期持仓的复核员，只做真实、可核查的判断，不要凭感觉。"
        f"{sym} 的机械入场条件已经满足({reason})，现价约${current_price:.2f}。"
        f"搜索一下这只股票最近几天有没有任何新的重大坏消息、基本面恶化迹象。"
        f"只有当你有真正的高确信度——催化剂/论文依然完整、没有新的重大反向证据"
        f"——才回答确信。如果有任何让你犹豫的新信息，回答等待，并说明原因"
        f"(这次不买不代表以后不买)。"
        f"最后一行必须是以下两种之一: DECISION: CONFIDENT 或 DECISION: WAIT。"
    )
    try:
        result = subprocess.run([CLAUDE_BIN, '-p', prompt, '--output-format', 'json'],
                                 capture_output=True, text=True, timeout=420,
                                 cwd='/data/qbao775/AlphaTrader')
        if result.returncode != 0:
            return False, f"confidence check call failed (rc={result.returncode}): {result.stderr[:200]}"
        data = json.loads(result.stdout)
        answer = data.get('result', '')
        confident = 'DECISION: CONFIDENT' in answer.upper()
        return confident, answer
    except Exception as e:
        return False, f"confidence check exception: {e}"


def _verify_fill(api, order_id, max_wait_sec=30, poll_sec=3):
    """Same pattern as mu_reentry.py: submit_order() only confirms
    acceptance, not a fill. Poll to a terminal status before trusting it."""
    import time
    waited = 0
    while waited < max_wait_sec:
        try:
            o = api.get_order(order_id)
        except Exception:
            time.sleep(poll_sec)
            waited += poll_sec
            continue
        if o.status == 'filled':
            return o
        if o.status in ('canceled', 'expired', 'rejected', 'suspended'):
            log(f"  order {order_id[:8]} reached terminal non-fill status: {o.status}")
            return None
        time.sleep(poll_sec)
        waited += poll_sec
    log(f"  order {order_id[:8]} did not reach a terminal status within {max_wait_sec}s")
    return None


def enter_position(api, state):
    px = get_live_price(api, 'SKHY')
    if not px:
        log("  no live SKHY price yet — will retry next tick")
        return

    now = datetime.datetime.utcnow()
    watch = state.get('watch')
    if not watch:
        # First observation — just start watching, don't buy yet.
        state['watch'] = {'first_seen_time': now.isoformat(), 'first_seen_price': px, 'lowest_price': px}
        save_state(state)
        log(f"  first price observed (${px:.2f}) — watching before buying, not chasing an open print")
        return

    watch['lowest_price'] = min(watch['lowest_price'], px)
    first_time = datetime.datetime.fromisoformat(watch['first_seen_time'])
    mins_waiting = (now - first_time).total_seconds() / 60
    recovery_from_low_pct = (px / watch['lowest_price'] - 1) * 100

    recovered = recovery_from_low_pct >= ENTRY_RECOVERY_FROM_LOW_PCT
    timed_out = mins_waiting >= ENTRY_MAX_WAIT_MIN
    # Bug fix (2026-07-12, user: "不要追涨"): the timeout fallback used to
    # force a buy regardless of price -- if the stock never actually dipped
    # and just kept climbing the whole wait window, that forced buy would be
    # chasing a rally, exactly what this logic exists to avoid. Only let the
    # timeout override fire if there was a REAL dip at some point (current
    # price not still above where we first started watching) -- otherwise
    # keep waiting rather than cave and chase.
    still_above_start = px >= watch['first_seen_price']
    if timed_out and still_above_start:
        watch['first_seen_time'] = now.isoformat()  # reset the clock, keep watching
        state['watch'] = watch
        save_state(state)
        log(f"  ⚠️ max wait elapsed but price (${px:.2f}) never dipped below where we started "
            f"watching (${watch['first_seen_price']:.2f}) — NOT chasing, resetting wait clock")
        return

    should_buy = recovered or timed_out
    if not should_buy:
        state['watch'] = watch
        save_state(state)
        log(f"  watching: current=${px:.2f} low_seen=${watch['lowest_price']:.2f} "
            f"recovery={recovery_from_low_pct:+.2f}% (need {ENTRY_RECOVERY_FROM_LOW_PCT}%) "
            f"waited={mins_waiting:.0f}min (max {ENTRY_MAX_WAIT_MIN}) — not buying yet")
        return

    reason = ("recovered off the observed low" if recovered
              else f"max wait ({ENTRY_MAX_WAIT_MIN}min) elapsed with a real dip seen, buying")

    last_check = watch.get('last_confidence_check')
    if last_check:
        mins_since = (now - datetime.datetime.fromisoformat(last_check)).total_seconds() / 60
        if mins_since < CONFIDENCE_RECHECK_COOLDOWN_MIN:
            log(f"  entry condition met ({reason}) but confidence re-check on cooldown "
                f"({mins_since:.0f}/{CONFIDENCE_RECHECK_COOLDOWN_MIN}min) — not buying yet")
            return
    watch['last_confidence_check'] = now.isoformat()
    state['watch'] = watch
    save_state(state)

    confident, reasoning = _confidence_check('SKHY', px, reason)
    if not confident:
        log(f"  entry condition met ({reason}) but confidence check said WAIT: {reasoning[:400]}")
        return
    log(f"  entry condition met ({reason}) AND confidence check CONFIRMED: {reasoning[:400]}")

    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    qty = int(min(target_notional, bp - 20) // px)
    if qty < 1:
        log(f"  insufficient buying power for even 1 share of SKHY @ ${px} — aborting")
        return

    with open(SIM_STATE_FILE, 'w') as f:
        json.dump({'intended_qty': qty, 'intended_price': px,
                    'intended_at': datetime.datetime.utcnow().isoformat()}, f)

    o = api.submit_order(symbol='SKHY', qty=qty, side='buy', type='market', time_in_force='day')
    filled = _verify_fill(api, o.id)
    if not filled:
        log(f"  ⚠ SKHY buy order={o.id[:8]} did NOT confirm as filled -- NOT marking entered.")
        send_email("⚠️ SKHY 下单未确认成交",
                    f"提交了买入订单(qty={qty} @~${px})但没能确认成交状态,"
                    f"没有标记为已建仓,下一个tick会重新判断是否需要重试。")
        return
    filled_qty_f, filled_px_f = float(filled.filled_qty), float(filled.filled_avg_price)
    log(f"  ✓ BOUGHT SKHY qty={filled_qty_f} @~${filled_px_f} order={o.id[:8]} status={filled.status}")
    with open(SIM_STATE_FILE, 'w') as f:
        json.dump({'intended_qty': qty, 'intended_price': px,
                    'filled_qty': filled_qty_f, 'filled_price': filled_px_f}, f)

    state = {'entered': True, 'entry_time': datetime.datetime.utcnow().isoformat(),
             'entry_price_est': filled_px_f, 'qty': filled_qty_f,
             'order_id': o.id}
    save_state(state)
    send_email("📈 SKHY 长期持仓 — 已建仓(已确认成交)",
               f"买入 SKHY {filled_qty_f}股,实际成交均价 ~${filled_px_f}"
               f"(等待理由: {reason}; 确信度检查: {reasoning[:200]})\n"
               f"目标价: ${TARGET_PRICE} 才卖出,中途不设止损,跌多少都不动。")


def manage_position(api):
    positions = [p for p in api.list_positions() if p.symbol == 'SKHY']
    if not positions:
        log("no SKHY position held (already sold or never filled) — marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat()}, f)
        return

    p = positions[0]
    current_px = float(p.market_value) / float(p.qty)
    plpc = float(p.unrealized_plpc) * 100
    log(f"  SKHY position: qty={p.qty} current=${current_px:.2f} unrealized_plpc={plpc:+.2f}% "
        f"(target=${TARGET_PRICE}, no stop-loss by design)")

    if current_px >= TARGET_PRICE:
        o = api.submit_order(symbol='SKHY', qty=p.qty, side='sell', type='market', time_in_force='day')
        # 2026-08-13, user: "不要到时候出现卖出但是没有卖出...的情况" -- verify
        # the sell actually filled before declaring done; a target-hit sell
        # that only got ACCEPTED (not filled) and got marked DONE_MARKER
        # anyway would leave a real position open with nothing watching it
        # anymore (main() short-circuits once DONE_MARKER exists).
        filled = _verify_fill(api, o.id)
        if not filled:
            log(f"  ⚠ SKHY sell order={o.id[:8]} did NOT confirm as filled -- NOT marking done, "
                f"will re-attempt next tick since the position is presumably still open.")
            send_email("⚠️ SKHY 目标价卖出未确认成交",
                        f"现价${current_px:.2f}已达目标${TARGET_PRICE},提交了卖出订单但没能确认成交,"
                        f"没有标记为已平仓,下一个tick会重新检查。")
            return
        filled_px_f = float(filled.filled_avg_price)
        log(f"  ✓ TARGET HIT — SOLD SKHY qty={filled.filled_qty} @~${filled_px_f} order={o.id[:8]} status={filled.status}")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'closed_at': datetime.datetime.utcnow().isoformat(),
                        'exit_price': filled_px_f, 'final_plpc': plpc,
                        'filled_qty': filled.filled_qty, 'order_id': o.id}, f, indent=2)
        send_email(f"🎯 SKHY 达到目标价 ${TARGET_PRICE} — 已卖出(已确认成交)",
                   f"实际成交价 ~${filled_px_f}\n最终盈亏: {plpc:+.2f}%")
    else:
        log(f"  未到目标价,继续持有(不设止损,这是刻意的设计)")


def main():
    if os.path.exists(DONE_MARKER):
        log("already sold (target hit) — nothing more to do")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    if not state.get('entered'):
        log("no position yet — attempting entry")
        enter_position(api, state)
    else:
        manage_position(api)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
