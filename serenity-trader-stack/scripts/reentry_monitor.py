#!/usr/bin/env python3
"""
reentry_monitor.py — monitors for Plan D re-entry conditions after the
2026-07-08 full liquidation to SGOV (Korea KOSPI/KOSDAQ sell-side sidecars
two days running + semiconductor "peak" fears + Iran strikes on US
facilities in Kuwait/Bahrain). User explicitly delegated the re-entry
TIMING decision: "如果你看到合适的时机请你可以入场，你来帮我打理" (2026-07-09).
See ~/serenity-trader-stack/PLAN_D.md for full context.

Runs daily (cron, pre-open). Checks 4 criteria:
  1. market_regime == RISK_ON, sustained for >= REENTRY_MIN_RISKON_DAYS
     consecutive daily checks (tracked in this script's own state file —
     a single good day is not enough, avoids whipsawing back in on a
     one-day bounce).
  2. KOSPI/KOSDAQ stabilized: no fresh >=4% single-day drop in the last
     3 sessions, and current level is not re-testing the crash lows.
  3+4. Qualitative: Middle East de-escalation + semiconductor "peak" debate
     clarity — free local Ollama pre-screen first; only escalates to paid
     claude -p if the quantitative gates (1+2) already passed AND the local
     read itself looks favorable. Same cost discipline as
     crossvalidate_satellite.py — never pay to ask "is it safe" if the
     hard numbers already say no.

If ALL checks clear: sells SGOV, redeploys 70/15/12/3 into SPY/QQQ/BRK.B/
cash sized to actual equity at execution time, removes the satellite-
buying pause file so crossvalidate_satellite.py's candidate screen resumes,
logs to PLAN_D.md-adjacent reports, emails a full report.

This executes REAL trades autonomously — only run while the user's
2026-07-09 delegation is current. If a future session is unsure whether
that delegation still holds (e.g., user explicitly paused it again, or a
new liquidation happened for a different reason), stop and ask rather than
assume this script's mandate is still valid.
"""
import sys, os, json, subprocess, datetime, re
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

STATE_FILE = '/home/qbao775/serenity-trader-stack/.reentry_state.json'
LOG_PATH = '/home/qbao775/serenity-trader-stack/reentry_monitor.log'
DONE_MARKER = '/home/qbao775/serenity-trader-stack/.reentry_executed'
PAUSE_FILE = '/home/qbao775/serenity-trader-stack/.SATELLITE_BUYING_PAUSED'
# 2026-07-13: user said "我什么时候让你买你再买" (only buy when I explicitly
# tell you to) -- standing policy, applies here too: all 4 re-entry gates
# clearing no longer auto-executes the core redeploy. Claude creates this
# file only in direct response to the user explicitly confirming re-entry.
CONFIRM_FILE = '/home/qbao775/serenity-trader-stack/.ENTRY_CONFIRMED_COREREENTRY'  # unused now, kept only as a manual override path
SIM_STATE_FILE = '/home/qbao775/serenity-trader-stack/.reentry_sim.json'

REENTRY_MIN_RISKON_DAYS = 5      # consecutive daily checks, not just one good day
KOREA_DROP_TRIGGER_PCT = -4.0    # a single-day move this bad = still unstable
KOSPI_CRASH_LOW = 7246.79        # 2026-07-08 close, the panic low
KOSDAQ_CRASH_LOW = 785.00
OLLAMA_HOST = 'http://localhost:11435'
OLLAMA_MODEL = 'gemma4:31b'

TARGETS = {'SPY': 0.70, 'QQQ': 0.15, 'BRK.B': 0.12}  # 3% cash implicit


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    line = f"[{ts}] {msg}"
    print(line, flush=True)


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


def check_market_regime(state):
    """Criterion 1: market_regime RISK_ON, sustained N consecutive checks."""
    from database import SessionLocal, get_setting
    db = SessionLocal()
    regime = get_setting(db, 'market_regime', 1, 'UNKNOWN')
    db.close()

    today = datetime.date.today().isoformat()
    last_check_date = state.get('last_regime_check_date')
    streak = state.get('riskon_streak', 0)

    if regime == 'RISK_ON':
        if last_check_date != today:
            streak += 1
    else:
        streak = 0

    state['riskon_streak'] = streak
    state['last_regime_check_date'] = today
    log(f"  [1] market_regime={regime}, RISK_ON streak={streak}/{REENTRY_MIN_RISKON_DAYS}")
    return regime == 'RISK_ON' and streak >= REENTRY_MIN_RISKON_DAYS


def check_korea_stability():
    """Criterion 2: no fresh sidecar-level drop, not re-testing crash lows."""
    import yfinance as yf
    try:
        kospi = yf.Ticker('^KS11').history(period='5d')['Close']
        kosdaq = yf.Ticker('^KQ11').history(period='5d')['Close']
    except Exception as e:
        log(f"  [2] Korea data fetch failed: {e} — treating as NOT stable")
        return False

    kospi_chg = kospi.pct_change().dropna() * 100
    kosdaq_chg = kosdaq.pct_change().dropna() * 100
    fresh_drop = (kospi_chg <= KOREA_DROP_TRIGGER_PCT).any() or (kosdaq_chg <= KOREA_DROP_TRIGGER_PCT).any()
    retesting_low = kospi.iloc[-1] <= KOSPI_CRASH_LOW * 1.02 or kosdaq.iloc[-1] <= KOSDAQ_CRASH_LOW * 1.02

    log(f"  [2] KOSPI={kospi.iloc[-1]:.2f} KOSDAQ={kosdaq.iloc[-1]:.2f} "
        f"fresh_sidecar_drop={fresh_drop} retesting_crash_low={retesting_low}")
    return not fresh_drop and not retesting_low


def ollama_call(prompt, timeout=240):
    try:
        import requests
        r = requests.post(f"{OLLAMA_HOST}/api/generate",
                          json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                          timeout=timeout)
        if r.status_code == 200:
            return r.json().get('response', '').strip()
    except Exception as e:
        log(f"  ollama_call failed: {e}")
    return ""


def search_recent_news(query, n=5):
    try:
        env = dict(os.environ)
        env["PATH"] = "/data/qbao775/miniconda3/bin:" + env.get("PATH", "")
        r = subprocess.run(['mcporter', 'call', 'exa.web_search_exa',
                            f"query={query}", f"numResults={n}"],
                           capture_output=True, text=True, timeout=90,
                           cwd='/data/qbao775/AlphaTrader', env=env)
        return r.stdout[:6000] if r.returncode == 0 else ""
    except Exception as e:
        log(f"  search_recent_news failed: {e}")
        return ""


def check_qualitative_conditions():
    """Criteria 3+4: Middle East + semiconductor-peak debate, free local
    pre-screen first, paid claude -p only if the local read is favorable."""
    me_news = search_recent_news("Iran US military conflict Middle East escalation latest news")
    semi_news = search_recent_news("semiconductor cycle peak memory chip outlook analysts latest")

    prompt = (
        "You are screening whether it's safe to re-enter US equities after a "
        "risk-off period triggered by (a) Iran-US military escalation in the "
        "Middle East and (b) semiconductor cycle 'peak' fears.\n\n"
        f"Recent Middle East news:\n{me_news[:3000]}\n\n"
        f"Recent semiconductor news:\n{semi_news[:3000]}\n\n"
        "Answer in this exact format:\n"
        "MIDDLE_EAST: <DEESCALATING/UNCHANGED/ESCALATING>\n"
        "SEMICONDUCTOR: <CLEARER/UNCHANGED/MORE_UNCERTAIN>\n"
        "REASON: <one clause>\n"
        "OVERALL: <SAFE_TO_REENTER/NOT_YET>\n"
    )
    local_take = ollama_call(prompt)
    log(f"  [3+4] local pre-screen:\n{local_take}")

    m = re.search(r'OVERALL:\s*(SAFE_TO_REENTER|NOT_YET)', local_take, re.I)
    local_verdict = m.group(1).upper() if m else 'NOT_YET'

    if local_verdict != 'SAFE_TO_REENTER':
        log("  [3+4] local pre-screen not favorable — not paying for deep-dive, NOT_YET")
        return False, local_take, ""

    # Local check looks favorable — get a real paid second opinion before
    # touching ~$61k. Same pattern as crossvalidate_satellite.py.
    deep_prompt = (
        "本地初筛认为可以考虑重新入场(Middle East 缓和 + 半导体周期看法更清晰),"
        "但这是一个真实的、约$6万美元的全仓再入场决定,请你做一次真正的核实判断:\n\n"
        f"本地初筛结果:\n{local_take}\n\n"
        f"中东局势最新消息:\n{me_news[:2000]}\n\n"
        f"半导体板块最新消息:\n{semi_news[:2000]}\n\n"
        "请给出3-5句话的独立判断:本地初筛靠谱吗?现在真的适合重新进场吗?"
        "给出明确结论 SAFE_TO_REENTER 或 NOT_YET。"
    )
    try:
        claude_bin = '/home/qbao775/.local/bin/claude'
        # 2026-08-13: a research-backed claude -p call turned out to need
        # 195-280s in practice elsewhere in this session (daily_retro.py
        # silently timed out at 180s for 2 full days before anyone noticed)
        # -- using the same 420s budget here rather than repeat that mistake,
        # especially since this call gates a real ~$60k re-entry decision.
        result = subprocess.run(
            [claude_bin, '-p', deep_prompt, '--output-format', 'json'],
            capture_output=True, text=True, timeout=420,
            cwd='/data/qbao775/AlphaTrader'
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            answer = data.get('result', '')
            cost = data.get('total_cost_usd', 0)
            log(f"  [3+4] claude -p cost ${cost:.4f}: {answer}")
            return bool(re.search(r'SAFE_TO_REENTER', answer, re.I)) and not re.search(r'NOT_YET', answer, re.I), local_take, answer
        else:
            log(f"  [3+4] claude -p failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"  [3+4] claude -p exception: {e}")
    return False, local_take, ""


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


def _verify_fill(api, order_id, max_wait_sec=30, poll_sec=3):
    """2026-08-13, user: '不要到时候出现卖出但是没有卖出，买入没有买入的情况'
    -- submit_order() only confirms acceptance, not a fill. This gates a
    ~$60k multi-leg re-entry (1 sell + up to 3 buys); a silently-unfilled
    leg here is exactly the failure mode to guard against. Same pattern as
    mu_reentry.py/skhy_position.py/meta_longhold.py."""
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


def execute_reentry(qualitative_note):
    api = get_alpaca()
    sgov = [p for p in api.list_positions() if p.symbol == 'SGOV']
    if not sgov:
        log("  no SGOV position found — nothing to re-enter from, aborting")
        return False

    # True pre-sale baseline, captured before any order is placed -- needed
    # below to detect once buying_power has actually caught up with the
    # confirmed sale, not just whether the order itself reached 'filled'.
    bp_before_sell = float(api.get_account().buying_power)

    qty = float(sgov[0].qty)
    sim_ledger = {'intended': {'SGOV_sell_qty': qty, 'targets': TARGETS}, 'actual': {}}
    with open(SIM_STATE_FILE, 'w') as f:
        json.dump(sim_ledger, f, indent=2)

    o = api.submit_order(symbol='SGOV', qty=qty, side='sell', type='market', time_in_force='day')
    filled = _verify_fill(api, o.id)
    if not filled:
        log(f"  ⚠ SGOV sell order={o.id[:8]} did NOT confirm as filled -- ABORTING re-entry entirely, "
            f"not proceeding to buy legs without confirmed funding.")
        send_email("⚠️ Plan D 重新入场中止 — SGOV卖出未确认成交",
                    f"提交了SGOV卖出订单(qty={qty})但没能确认成交状态,已中止整个重新入场流程,"
                    f"没有标记为已执行,下一个tick会重新检查条件。")
        return False
    sgov_sold_qty = float(filled.filled_qty)
    sgov_proceeds_est = sgov_sold_qty * float(filled.filled_avg_price)
    log(f"  ✓ SOLD SGOV {sgov_sold_qty}sh @~${filled.filled_avg_price} order={o.id[:8]} status={filled.status}")
    sim_ledger['actual']['SGOV_sell'] = {'qty': sgov_sold_qty, 'price': float(filled.filled_avg_price)}
    with open(SIM_STATE_FILE, 'w') as f:
        json.dump(sim_ledger, f, indent=2)

    # 2026-08-13: confirming the ORDER is 'filled' doesn't mean the ACCOUNT's
    # buying_power has caught up yet -- this exact settlement lag already
    # broke 5 mirror buys once elsewhere in this codebase (daily_open_
    # daytrade.py's comment: "a fixed 6s sleep raced fill settlement"). Poll
    # against the TRUE pre-sale baseline (captured before the order was even
    # submitted) until buying_power actually reflects the proceeds, or give
    # up after a bounded wait and proceed with whatever is really available
    # (never invent notional beyond what buying_power actually shows).
    import time as _time
    acc = api.get_account()
    for _ in range(15):
        acc = api.get_account()
        bp = float(acc.buying_power)
        if bp >= bp_before_sell + sgov_proceeds_est * 0.9:
            break
        _time.sleep(3)
    total = float(acc.equity)
    bp = float(acc.buying_power)
    log(f"  post-sell equity=${total:.2f} bp=${bp:.2f} (SGOV proceeds ~${sgov_proceeds_est:.2f}, "
        f"pre-sale baseline was ${bp_before_sell:.2f})")

    import market_data as md
    orders = []
    unfilled = []
    for sym, weight in TARGETS.items():
        notional = min(total * weight, bp - 100)
        if notional < 10:
            continue
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            log(f"  no price for {sym}, skipping")
            continue
        qty_buy = round(notional / px, 4)
        o = api.submit_order(symbol=sym, qty=qty_buy, side='buy', type='market', time_in_force='day')
        filled = _verify_fill(api, o.id)
        if not filled:
            log(f"  ⚠ {sym} buy order={o.id[:8]} did NOT confirm as filled -- leg left unfilled, "
                f"continuing with the remaining legs rather than aborting (SGOV proceeds are already "
                f"real cash sitting in buying power either way).")
            unfilled.append(sym)
            continue
        filled_qty_buy = float(filled.filled_qty)
        filled_px_buy = float(filled.filled_avg_price)
        log(f"  ✓ BUY {sym} qty={filled_qty_buy} @~${filled_px_buy} order={o.id[:8]} status={filled.status}")
        orders.append(f"{sym}: {filled_qty_buy}sh (~${filled_qty_buy * filled_px_buy:.2f})")
        sim_ledger['actual'][sym] = {'qty': filled_qty_buy, 'price': filled_px_buy}
        bp -= notional

    with open(SIM_STATE_FILE, 'w') as f:
        json.dump(sim_ledger, f, indent=2)

    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
        log("  ✓ removed .SATELLITE_BUYING_PAUSED — satellite candidate screening resumes")
    if os.path.exists(CONFIRM_FILE):
        os.remove(CONFIRM_FILE)

    with open(DONE_MARKER, 'w') as f:
        json.dump({'executed_at': datetime.datetime.utcnow().isoformat(),
                    'equity_at_reentry': total, 'orders': orders, 'unfilled_legs': unfilled}, f, indent=2)

    unfilled_note = f"\n\n⚠️ 以下标的下单未确认成交,需要人工检查: {', '.join(unfilled)}" if unfilled else ""
    body = (f"Plan D 重新入场执行完成 ({datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC})\n\n"
            f"账户 equity: ${total:.2f}\n"
            f"卖出 SGOV {sgov_sold_qty}股(已确认成交),买回:\n" + "\n".join(orders) + unfilled_note + "\n\n"
            f"判断依据:\n{qualitative_note}\n\n"
            f"卫星仓自动筛选(含 EWY)已恢复。")
    send_email("✅ Plan D 重新入场完成" + ("(部分未成交,见正文)" if unfilled else ""), body)
    log("─── 重新入场完成 ───" + (f" (未成交: {unfilled})" if unfilled else ""))
    return True


def main():
    if os.path.exists(DONE_MARKER):
        log("already re-entered (marker exists) — nothing to do, this script has done its job")
        return

    state = load_state()
    log("checking re-entry conditions...")

    regime_ok = check_market_regime(state)
    save_state(state)
    if not regime_ok:
        log("[1] not met — skipping remaining checks (cheapest gate first)")
        return

    korea_ok = check_korea_stability()
    if not korea_ok:
        log("[2] not met — skipping qualitative checks")
        return

    log("[1] and [2] both pass — running qualitative check (free local first, paid only if favorable)")
    qual_ok, local_take, deep_take = check_qualitative_conditions()
    if not qual_ok:
        log("[3+4] not met — NOT re-entering this cycle")
        return

    log("🟢 ALL CONDITIONS MET — executing re-entry")
    # 2026-08-13: the 2026-07-13 confirm-before-buy policy is REVERSED (user
    # restored full autonomy across satellite/core-reentry/SKHY/MU/META).
    # Unlike those single-name scripts, this one doesn't need a NEW separate
    # confidence-check bolt-on -- criteria 3+4 above already ARE a genuine,
    # research-backed judgment call (local pre-screen + a paid claude -p
    # deep-verification requiring explicit SAFE_TO_REENTER), not a bare
    # mechanical price trigger. Execute directly once all 4 gates clear.
    execute_reentry(deep_take or local_take)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
