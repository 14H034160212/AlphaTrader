#!/usr/bin/env python3
"""
mu_reentry.py — re-enter MU (Micron), user-directed (2026-07-11): "可以都买，
你决定" (buy both MU and SKHY, size at Claude's discretion).

Important context Claude flagged before executing: MU was explicitly
REJECTED on 2026-07-02 during a systematic screen as a classic memory-cycle
valuation trap (cheap forward PE reflecting peak-cycle earnings that
historically mean-revert once supply catches up) — see
~/serenity-trader-stack/PLAN_D.md and project_management_mandate.md memory.
The 2026-07-09/11 news (Micron's $3B US supply-chain investment, Trump's
$250B figure, BofA/UBS bullish reiterations, DRAM pricing forecast raised
17%->32% QoQ) is real, but doesn't resolve that original valuation-trap
concern -- it's the same "AI demand is structural not cyclical" narrative
in a new news wrapper. This is a considered REVERSAL of a prior rejection,
not a fresh uncontested thesis.

Given that unresolved risk, Claude sized this more conservatively than the
SKHY position (5% vs 20%) and initially added a self-imposed -15%
stop-loss. User then explicitly said "我觉得不需要设置止损" (no stop-loss
needed) -- removed. MU is now a pure hold with NO defined exit condition
at all (not even a take-profit target like SKHY's $200) -- the only thing
watching it is crossvalidate_satellite.py's regular 4h thesis recheck,
which can escalate/recommend TRIM/EXIT but does not auto-sell. This script
now only handles entry; there is no ongoing management logic left to run
after that.
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

STATE_FILE = '/home/qbao775/serenity-trader-stack/.mu_reentry_state.json'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.mu_reentry_entered'
# 2026-08-13: the 2026-07-13 policy ("我什么时候让你买你再买" -- only buy
# when explicitly told) is REVERSED -- user restored full autonomous buying
# across SKHY/MU/META/satellite-screen/core-reentry, the same scope 07-13
# had narrowed, back to the original 07-07 grant. But immediately added the
# caveat "但是你要确保很有信心才买" (but you must be confident before
# buying) -- so removing the human-confirmation-file gate must NOT mean
# firing the instant the mechanical price condition (recovery+stability)
# is met; that condition is a chase-guard, not the full judgment. Replaced
# with a genuine, current, research-backed confidence check (mirrors
# daily_open_daytrade.py's own picker: search + claude -p judgment +
# 宁缺毋滥 default-to-no) run at the moment of the mechanical trigger.
CONFIRM_FILE = '/home/qbao775/serenity-trader-stack/.ENTRY_CONFIRMED_MU'  # unused now, kept only as a manual override path
CLAUDE_BIN = '/home/qbao775/.local/bin/claude'
CONFIDENCE_RECHECK_COOLDOWN_MIN = 60  # don't re-run the (paid, web-search) confidence check every tick once it's said WAIT
# 2026-08-13, user: "模拟盘和实盘同步开启" -- add a paper-ledger record
# alongside the real order, same principle as daily_open_daytrade.py's
# sim_positions vs live reconciliation. This script only does a single
# one-time entry (no ongoing intraday rebalancing), so the paper ledger is
# simple: record what was INTENDED (qty/price at decision time) before
# submitting, then what was ACTUALLY filled after -- an independent record
# to check the real fill against, not just trust the order call succeeded.
SIM_STATE_FILE = '/home/qbao775/serenity-trader-stack/.mu_reentry_sim_position.json'

TARGET_PCT = 0.05         # 5% -- more conservative than SKHY's 20%, given
                          # the unresolved valuation-trap concern
# STOP_LOSS_PCT removed 2026-07-11 -- user: "我觉得不需要设置止损".
# No downside limit on this position at all now.

# 2026-07-11: user asked to watch first and prefer a pullback before buying
# ("你可以先看一下，最好等回落一些再买"), then added: "上周五冲高回落，有资金
# 在借利好卖出，要等回落走稳了再买" -- some holders are selling into the good
# news, so wait for the price to actually STABILIZE, not just bounce once.
# Approximation, not a guarantee: require (a) price off its observed low by
# some margin AND (b) no new low in the last few checks (proxy for "selling
# pressure has actually let up"), or a max wait so this doesn't wait forever.
ENTRY_RECOVERY_FROM_LOW_PCT = 1.5
ENTRY_STABLE_CHECKS_REQUIRED = 2   # consecutive checks with no new low
ENTRY_MAX_WAIT_MIN = 120


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


def _confidence_check(sym, current_price, reason):
    """2026-08-13, user restored full autonomous buying (reversing the
    2026-07-13 confirm-before-buy policy) but immediately added: '但是你要
    确保很有信心才买' (but you must be confident before buying). The
    mechanical entry condition (recovery+stability off a low) is a chase-
    guard, not the full judgment -- it says nothing about whether anything
    has changed in the underlying thesis since. This replaces the removed
    human-confirmation-file checkpoint with a fresh, research-backed
    judgment call, mirroring daily_open_daytrade.py's own picker pattern
    (search + claude -p + 宁缺毋滥 default-to-no, never rubber-stamp a
    price-only trigger)."""
    import subprocess
    prompt = (
        f"你是一个长期价值投资的复核员，只做真实、可核查的判断，不要凭感觉。"
        f"{sym} 的机械入场条件已经满足({reason})，现价约${current_price:.2f}。"
        f"搜索一下这只股票最近几天有没有任何新的重大坏消息、基本面恶化迹象，"
        f"或者原来买入逻辑里已知的风险点(比如周期性行业的估值陷阱、需求可持续性)"
        f"有没有新的证据显示正在应验。"
        f"只有当你有真正的高确信度——催化剂/论文依然完整、没有新的重大反向证据"
        f"——才回答确信。如果有任何让你犹豫的新信息，或者你只是觉得'差不多可以'"
        f"但没有真正的把握，回答等待，并说明原因(这次不买不代表以后不买，"
        f"下次机械条件再触发时会重新评估)。"
        f"最后一行必须是以下两种之一: DECISION: CONFIDENT 或 DECISION: WAIT。"
    )
    try:
        # 2026-08-13: daily_retro.py's own research-backed claude -p call
        # (same shape: web search + reasoning) turned out to need 195-280s in
        # practice, well past a 180s timeout -- learned that the hard way
        # (it silently failed for 2 full days before anyone noticed). Using
        # the same 420s budget here instead of repeating that mistake.
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
    """2026-08-13, user: '不要到时候出现卖出但是没有卖出，买入没有买入的情况'
    (don't let a sell-that-didn't-sell or buy-that-didn't-buy situation
    happen) -- submit_order() only confirms the order was ACCEPTED, not that
    it actually FILLED. Poll until it reaches a terminal state; only a
    genuinely 'filled' order should ever be treated as a real position
    change. Returns the filled order object, or None if it didn't fill
    (rejected/canceled/expired/still pending after the wait)."""
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
    import market_data as md
    q = md.get_stock_quote('MU')
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live MU price available yet — will retry next tick")
        return

    now = datetime.datetime.utcnow()
    watch = state.get('watch')
    if not watch:
        state['watch'] = {'first_seen_time': now.isoformat(), 'first_seen_price': px,
                           'lowest_price': px, 'stable_checks': 0}
        save_state(state)
        log(f"  first price observed (${px:.2f}) — watching for stabilization, not chasing the open")
        return

    made_new_low = px < watch['lowest_price']
    watch['lowest_price'] = min(watch['lowest_price'], px)
    watch['stable_checks'] = 0 if made_new_low else watch['stable_checks'] + 1

    first_time = datetime.datetime.fromisoformat(watch['first_seen_time'])
    mins_waiting = (now - first_time).total_seconds() / 60
    recovery_pct = (px / watch['lowest_price'] - 1) * 100

    recovered_and_stable = (recovery_pct >= ENTRY_RECOVERY_FROM_LOW_PCT
                             and watch['stable_checks'] >= ENTRY_STABLE_CHECKS_REQUIRED)
    timed_out = mins_waiting >= ENTRY_MAX_WAIT_MIN
    # Bug fix (2026-07-12, user: "不要追涨"): don't let the timeout override
    # force a buy if the price never actually dipped below where we started
    # watching -- that would be chasing a continued rally, not buying a
    # stabilized pullback. Reset the clock and keep waiting instead.
    still_above_start = px >= watch['first_seen_price']
    if timed_out and still_above_start:
        watch['first_seen_time'] = now.isoformat()
        state['watch'] = watch
        save_state(state)
        log(f"  ⚠️ max wait elapsed but price (${px:.2f}) never dipped below where we started "
            f"watching (${watch['first_seen_price']:.2f}) — NOT chasing, resetting wait clock")
        return

    if not (recovered_and_stable or timed_out):
        state['watch'] = watch
        save_state(state)
        log(f"  watching: current=${px:.2f} low=${watch['lowest_price']:.2f} recovery={recovery_pct:+.2f}% "
            f"stable_checks={watch['stable_checks']}/{ENTRY_STABLE_CHECKS_REQUIRED} "
            f"waited={mins_waiting:.0f}/{ENTRY_MAX_WAIT_MIN}min — not buying yet")
        return

    reason = "recovered and stabilized off the observed low" if recovered_and_stable else f"max wait ({ENTRY_MAX_WAIT_MIN}min) elapsed with a real dip seen, buying"

    # 2026-08-13: mechanical trigger firing is necessary but not sufficient --
    # require a fresh, research-backed confidence check right now, and cool
    # down between checks so a persistent WAIT doesn't re-run the (paid,
    # web-search) check every single tick.
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

    confident, reasoning = _confidence_check('MU', px, reason)
    if not confident:
        log(f"  entry condition met ({reason}) but confidence check said WAIT: {reasoning[:400]}")
        return
    log(f"  entry condition met ({reason}) AND confidence check CONFIRMED: {reasoning[:400]}")

    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    qty = round(min(target_notional, bp - 20) / px, 4)
    if qty <= 0:
        log(f"  insufficient buying power for MU @ ${px} — aborting")
        return

    # Paper-ledger record of INTENT, written before the real order -- gives
    # an independent record to check the real fill against afterward.
    with open(SIM_STATE_FILE, 'w') as f:
        json.dump({'intended_qty': qty, 'intended_price': px,
                    'intended_at': datetime.datetime.utcnow().isoformat()}, f)

    o = api.submit_order(symbol='MU', qty=qty, side='buy', type='market', time_in_force='day')
    filled = _verify_fill(api, o.id)
    if not filled:
        log(f"  ⚠ MU buy order={o.id[:8]} did NOT confirm as filled -- NOT marking entered, "
            f"NOT sending a success email. Will re-attempt next tick if still flat.")
        send_email("⚠️ MU 下单未确认成交",
                    f"提交了买入订单(qty={qty} @~${px})但没能确认成交状态,"
                    f"没有标记为已建仓,下一个tick会重新判断是否需要重试。")
        return
    log(f"  ✓ BOUGHT MU qty={qty} @~${px} order={o.id[:8]} status={filled.status} filled_qty={filled.filled_qty}")

    # Reconcile the real fill against the paper-ledger intent recorded above.
    filled_qty_f, filled_px_f = float(filled.filled_qty), float(filled.filled_avg_price)
    qty_drift_pct = (filled_qty_f - qty) / qty * 100 if qty else 0.0
    px_drift_pct = (filled_px_f - px) / px * 100 if px else 0.0
    with open(SIM_STATE_FILE, 'w') as f:
        json.dump({'intended_qty': qty, 'intended_price': px,
                    'filled_qty': filled_qty_f, 'filled_price': filled_px_f,
                    'qty_drift_pct': round(qty_drift_pct, 3), 'px_drift_pct': round(px_drift_pct, 3)}, f)
    if abs(qty_drift_pct) > 5 or abs(px_drift_pct) > 3:
        log(f"  ⚠ real fill diverged notably from intent: qty {qty_drift_pct:+.1f}%, price {px_drift_pct:+.1f}%")

    with open(DONE_MARKER, 'w') as f:
        json.dump({'entered_at': datetime.datetime.utcnow().isoformat(),
                    'order_id': o.id, 'filled_qty': filled.filled_qty,
                    'filled_avg_price': filled.filled_avg_price}, f)
    save_state({'entry_price_est': px, 'qty': qty})
    send_email("📈 MU 重新建仓(已确认成交)",
               f"买入 MU {filled.filled_qty}股,实际成交均价 ~${filled.filled_avg_price}"
               f"(等待理由: {reason}; 确信度检查: {reasoning[:200]})\n"
               f"不设止损、不设止盈目标(用户明确要求不设止损)\n"
               f"后续由 crossvalidate_satellite.py 的常规4小时论文复核自动跟踪,"
               f"该机制只会提示/升级,不会自动卖出。")


def main():
    if os.path.exists(DONE_MARKER):
        log("MU already entered — no stop-loss, no take-profit target, nothing left for this "
            "script to do. Ongoing monitoring is crossvalidate_satellite.py's job.")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    state = load_state()
    log("no MU position yet — attempting entry")
    enter_position(api, state)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
