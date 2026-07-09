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
        result = subprocess.run(
            [claude_bin, '-p', deep_prompt, '--output-format', 'json'],
            capture_output=True, text=True, timeout=180,
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
    key = os.environ.get('RESEND_API_KEY')
    if not key:
        log("email skipped: RESEND_API_KEY not set in env")
        return
    try:
        import requests
        r = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'from': 'onboarding@resend.dev', 'to': ['bqmbill714@gmail.com'],
                  'subject': subject, 'text': body}, timeout=15)
        log(f"email: {r.status_code}")
    except Exception as e:
        log(f"email err: {e}")


def execute_reentry(qualitative_note):
    api = get_alpaca()
    sgov = [p for p in api.list_positions() if p.symbol == 'SGOV']
    if not sgov:
        log("  no SGOV position found — nothing to re-enter from, aborting")
        return False

    qty = float(sgov[0].qty)
    o = api.submit_order(symbol='SGOV', qty=qty, side='sell', type='market', time_in_force='day')
    log(f"  ✓ SOLD SGOV {qty}sh order={o.id[:8]}")

    import time
    time.sleep(10)
    acc = api.get_account()
    total = float(acc.equity)
    bp = float(acc.buying_power)
    log(f"  post-sell equity=${total:.2f} bp=${bp:.2f}")

    import market_data as md
    orders = []
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
        log(f"  ✓ BUY {sym} qty={qty_buy} notional=${notional:.2f} order={o.id[:8]}")
        orders.append(f"{sym}: {qty_buy}sh (~${notional:.2f})")
        bp -= notional

    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
        log("  ✓ removed .SATELLITE_BUYING_PAUSED — satellite candidate screening resumes")

    with open(DONE_MARKER, 'w') as f:
        json.dump({'executed_at': datetime.datetime.utcnow().isoformat(),
                    'equity_at_reentry': total, 'orders': orders}, f, indent=2)

    body = (f"Plan D 重新入场执行完成 ({datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC})\n\n"
            f"账户 equity: ${total:.2f}\n"
            f"卖出 SGOV {qty}股,买回:\n" + "\n".join(orders) + "\n\n"
            f"判断依据:\n{qualitative_note}\n\n"
            f"卫星仓自动筛选(含 EWY)已恢复。")
    send_email("✅ Plan D 重新入场完成", body)
    log("─── 重新入场完成 ───")
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
    execute_reentry(deep_take or local_take)


if __name__ == '__main__':
    main()
