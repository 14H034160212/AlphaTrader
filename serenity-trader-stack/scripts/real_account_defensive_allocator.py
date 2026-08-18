#!/usr/bin/env python3
"""
real_account_defensive_allocator.py -- dynamic SPY <-> SGOV (short-term
treasuries) allocation for the REAL account, user-directed 2026-08-18:
"美股实盘如果还在下跌的话可以先不要买标普500，全部买买债，你可以每天判断
要不要买入，什么时候买入" (if the real account keeps declining, hold off
on SPY and go to bonds instead; judge daily whether/when to buy back in).

This runs ON TOP OF the 2026-08-13 SPY-only de-risking (CLAUDE.md) -- the
real account still holds no individual stocks, this only decides between
SPY (growth) and SGOV (defensive parking) day to day. Not gated by
.REAL_TRADING_PAUSED_SPY_ONLY -- that pause is specifically about NOT
adding new individual stock positions (MU/SKHY/META/satellite/reentry);
this script never buys anything but SPY/SGOV.

Design, deliberately NOT a single-day mechanical trigger (checked SPY's
actual data 2026-08-18: it's still +1.97% ABOVE its own 20-day moving
average despite the recent 3-day soft stretch that prompted this request --
a same-day/1-2-day dip is normal noise, not a regime worth a defensive move,
and a bare moving-average crossover would rotate expensively on every
minor wiggle):

  1. Quantitative pre-filter: SPY meaningfully below its 20-day moving
     average (DEFENSIVE_MA_BUFFER_PCT) is required before even considering
     a defensive move -- this is necessary, not sufficient.
  2. Only if (1) fires: a genuine, research-backed claude -p judgment call
     (same pattern validated in mu_reentry.py etc. on 2026-08-13) asking
     whether this looks like a real risk-off regime or normal volatility
     to ride out. Defaults to STAY_IN_SPY on any ambiguity or call failure
     (宁缺毋滥 applied to de-risking, not just to buying).
  3. To move BACK from SGOV to SPY: requires SPY back above its 20-day MA
     AND a few consecutive stable/non-declining checks (same "recovered
     and stabilized" pattern as the long-term entry scripts -- don't buy
     back on the first green tick), THEN the same kind of genuine judgment
     call before actually re-entering.
  4. All order execution uses fill verification (poll to a confirmed
     'filled' status) -- same standard as every other script touched today.
  5. Quiet unless something changes: emails only on an actual regime
     switch, not on every routine "still fine" check.

Cron: once per day, shortly after open (this is a slow, regime-level
decision, not an intraday one -- checking every 5 minutes would just add
noise and cost without adding signal).
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.real_defensive_allocator_state.json'
CLAUDE_BIN = '/home/qbao775/.local/bin/claude'

DEFENSIVE_MA_BUFFER_PCT = 2.0    # SPY must be at least this far below its 20d MA before
                                 # even considering a defensive move -- filters normal noise
MA_WINDOW_DAYS = 20
STABLE_CHECKS_TO_REENTER = 3     # consecutive daily checks back above the MA before
                                 # even asking the confidence question about re-entering


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
        log("email skipped: sender/pw/recipient not set in DB")
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


def _verify_fill(api, order_id, max_wait_sec=30, poll_sec=3):
    """Same standard applied to every script touched 2026-08-13: submit_order()
    only confirms acceptance, not a fill. Poll to a terminal status."""
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


def get_spy_vs_ma(api):
    """Returns (current_price, ma20, pct_below_ma) -- pct_below_ma is
    positive when SPY is BELOW the MA (i.e. weak), negative when above."""
    from database import SessionLocal, get_setting
    import requests
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    h = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}
    end = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    start = (datetime.datetime.utcnow() - datetime.timedelta(days=MA_WINDOW_DAYS * 2)).strftime('%Y-%m-%dT00:00:00Z')
    r = requests.get('https://data.alpaca.markets/v2/stocks/SPY/bars', headers=h,
                      params={'start': start, 'end': end, 'timeframe': '1Day', 'feed': 'iex', 'limit': 100},
                      timeout=20)
    bars = r.json().get('bars', [])
    closes = [b['c'] for b in bars]
    if len(closes) < MA_WINDOW_DAYS:
        return None, None, None
    ma = sum(closes[-MA_WINDOW_DAYS:]) / MA_WINDOW_DAYS
    current = closes[-1]
    pct_below = (ma - current) / ma * 100
    return current, ma, pct_below


def _confidence_check(direction, context):
    """direction: 'defensive' (considering SPY->SGOV) or 'reentry' (considering
    SGOV->SPY). Genuine research-backed judgment, same pattern validated
    2026-08-13 -- defaults to the CONSERVATIVE choice on any ambiguity or
    failure: for 'defensive' that's STAY_IN_SPY (don't flee on noise), for
    'reentry' that's STAY_IN_SGOV (don't buy back into a false bounce)."""
    import subprocess
    if direction == 'defensive':
        prompt = (
            f"你是一个防御性资产配置的复核员，只做真实、可核查的判断。"
            f"账户目前持有SPY作为唯一仓位。定量信号显示:{context}\n\n"
            f"搜索一下最近的市场消息(宏观数据、Fed政策、地缘政治、盈利季表现等)，"
            f"判断这是不是一次真正值得防御性撤退(卖出SPY换成短期美债SGOV)的下跌，"
            f"还是正常的市场波动/回调,继续持有反而更合理。"
            f"只有当你找到真实、具体的证据支持'这是一次实质性的风险离场信号'时才回答"
            f"应该防御；如果证据不够充分、或者只是正常波动，回答继续持有SPY"
            f"(这次不撤不代表以后不会撤，下次信号再触发时会重新评估)。"
            f"最后一行必须是以下两种之一: DECISION: GO_DEFENSIVE 或 DECISION: STAY_IN_SPY。"
        )
    else:
        prompt = (
            f"你是一个防御性资产配置的复核员，只做真实、可核查的判断。"
            f"账户目前防御性持有SGOV(短期美债)，SPY已经企稳回升。定量信号显示:{context}\n\n"
            f"搜索一下最近的市场消息，判断这是不是一次真正值得重新买入SPY的企稳，"
            f"还是可能的假反弹(逢高遇阻后可能继续下跌)。"
            f"只有当你找到真实、具体的证据支持'企稳是真实的、值得重新入场'时才回答"
            f"应该买入；如果证据不够充分、担心是假反弹，回答继续持有SGOV等待"
            f"(这次不买不代表以后不会买，下次信号再触发时会重新评估)。"
            f"最后一行必须是以下两种之一: DECISION: REENTER_SPY 或 DECISION: STAY_IN_SGOV。"
        )
    try:
        result = subprocess.run([CLAUDE_BIN, '-p', prompt, '--output-format', 'json'],
                                 capture_output=True, text=True, timeout=420,
                                 cwd='/data/qbao775/AlphaTrader')
        if result.returncode != 0:
            return False, f"confidence check call failed (rc={result.returncode}): {result.stderr[:200]}"
        data = json.loads(result.stdout)
        answer = data.get('result', '')
        decided_to_act = ('DECISION: GO_DEFENSIVE' in answer.upper() or 'DECISION: REENTER_SPY' in answer.upper())
        return decided_to_act, answer
    except Exception as e:
        return False, f"confidence check exception: {e}"


def _rotate(api, sell_symbol, buy_symbol):
    """Sells 100% of sell_symbol, buys buy_symbol with the confirmed
    proceeds. Fill-verified at every step; aborts (doesn't half-execute)
    if the sell doesn't confirm."""
    positions = {p.symbol: p for p in api.list_positions()}
    if sell_symbol not in positions:
        log(f"  no {sell_symbol} position to sell -- nothing to rotate")
        return False
    qty = positions[sell_symbol].qty
    o = api.submit_order(symbol=sell_symbol, qty=qty, side='sell', type='market', time_in_force='day')
    filled = _verify_fill(api, o.id)
    if not filled:
        log(f"  ⚠ {sell_symbol} sell did NOT confirm filled -- aborting rotation")
        send_email(f"⚠️ 防御性调仓中止 — {sell_symbol}卖出未确认成交",
                    f"提交了{sell_symbol}卖出订单但没能确认成交状态，已中止调仓，下一个tick会重新判断。")
        return False
    log(f"  ✓ SOLD {sell_symbol} qty={filled.filled_qty} @~${filled.filled_avg_price}")

    import market_data as md
    import time as _time
    bp = None
    for _ in range(15):
        acc = api.get_account()
        bp = float(acc.buying_power)
        if bp > 100:
            break
        _time.sleep(3)
    q = md.get_stock_quote(buy_symbol)
    px = q['current'] if q and q.get('current') else None
    if not px or not bp:
        log(f"  ⚠ couldn't get price for {buy_symbol} or buying power never settled -- "
            f"{sell_symbol} is sold but {buy_symbol} buy not placed, will retry next tick")
        return False
    qty_buy = round((bp - 5) / px, 4)
    if qty_buy <= 0:
        log(f"  ⚠ insufficient buying power (${bp:.2f}) to buy {buy_symbol} -- will retry next tick")
        return False
    o2 = api.submit_order(symbol=buy_symbol, qty=qty_buy, side='buy', type='market', time_in_force='day')
    filled2 = _verify_fill(api, o2.id)
    if not filled2:
        log(f"  ⚠ {buy_symbol} buy did NOT confirm filled -- {sell_symbol} already sold, "
            f"cash is sitting uninvested, will retry the {buy_symbol} buy next tick")
        send_email(f"⚠️ 防御性调仓部分完成 — {buy_symbol}买入未确认成交",
                    f"{sell_symbol}已确认卖出，但{buy_symbol}买入没能确认成交，资金暂时是现金，"
                    f"下一个tick会重试买入。")
        return False
    log(f"  ✓ BOUGHT {buy_symbol} qty={filled2.filled_qty} @~${filled2.filled_avg_price}")
    return True


def main():
    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    mode = state.get('mode', 'SPY')  # 'SPY' or 'SGOV'

    current, ma, pct_below = get_spy_vs_ma(api)
    if current is None:
        log("  couldn't compute SPY vs its moving average (insufficient data) — skipping this tick")
        return
    log(f"  SPY=${current:.2f} MA{MA_WINDOW_DAYS}=${ma:.2f} pct_below_MA={pct_below:+.2f}% mode={mode}")

    if mode == 'SPY':
        if pct_below < DEFENSIVE_MA_BUFFER_PCT:
            log(f"  SPY not meaningfully below its MA (need >={DEFENSIVE_MA_BUFFER_PCT}%) — staying in SPY, no check needed")
            state['stable_checks_above_ma'] = 0
            save_state(state)
            return
        context = f"SPY现价${current:.2f}，20日均线${ma:.2f}，低于均线{pct_below:.2f}%"
        should_act, reasoning = _confidence_check('defensive', context)
        if not should_act:
            log(f"  quantitative signal fired but confidence check said STAY_IN_SPY: {reasoning[:400]}")
            return
        log(f"  confidence check CONFIRMED defensive move: {reasoning[:400]}")
        if _rotate(api, 'SPY', 'SGOV'):
            state['mode'] = 'SGOV'
            state['stable_checks_above_ma'] = 0
            save_state(state)
            send_email("🛡️ 实盘防御性调仓 — 已转为SGOV(短期美债)",
                       f"SPY低于20日均线{pct_below:.2f}%，判断为真实风险离场信号，已卖出SPY换成SGOV。\n"
                       f"判断依据: {reasoning[:500]}")

    else:  # mode == 'SGOV'
        if pct_below >= 0:
            log(f"  SPY still below its MA ({pct_below:+.2f}%) — staying defensive in SGOV")
            state['stable_checks_above_ma'] = 0
            save_state(state)
            return
        stable = state.get('stable_checks_above_ma', 0) + 1
        state['stable_checks_above_ma'] = stable
        save_state(state)
        if stable < STABLE_CHECKS_TO_REENTER:
            log(f"  SPY back above its MA but only {stable}/{STABLE_CHECKS_TO_REENTER} consecutive stable "
                f"checks — waiting for more confirmation before even asking the re-entry question")
            return
        context = f"SPY现价${current:.2f}，20日均线${ma:.2f}，已连续{stable}次检查保持在均线上方"
        should_act, reasoning = _confidence_check('reentry', context)
        if not should_act:
            log(f"  stabilization confirmed but confidence check said STAY_IN_SGOV: {reasoning[:400]}")
            return
        log(f"  confidence check CONFIRMED re-entry: {reasoning[:400]}")
        if _rotate(api, 'SGOV', 'SPY'):
            state['mode'] = 'SPY'
            state['stable_checks_above_ma'] = 0
            save_state(state)
            send_email("📈 实盘重新买入SPY — 已转出SGOV",
                       f"SPY已连续{stable}次检查企稳在20日均线上方，判断为真实企稳，已卖出SGOV换回SPY。\n"
                       f"判断依据: {reasoning[:500]}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
