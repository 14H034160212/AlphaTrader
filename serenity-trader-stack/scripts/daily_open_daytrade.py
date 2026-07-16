#!/usr/bin/env python3
"""
daily_open_daytrade.py — STANDING daily day-trade automation (NOT date-scoped,
unlike plan_d_daytrade_20260715.py / news_catalyst_daytrade_20260715.py, which
this is built from and generalizes).

2026-07-16: user asked Claude to "养成自己独立美股开盘操作" (form the habit of
independently operating at the US market open every day) -- picking suitable
stocks based on that day's news/pre-market conditions, without a fresh
per-day confirmation. This REVERSES the 2026-07-13 "confirm before every
buy" policy SPECIFICALLY for this daily open-to-close day-trading activity.
Other buy paths -- the satellite CANDIDATE_WATCHLIST screen, SKHY/MU/META
long-term re-entry, Plan D core re-entry via reentry_monitor.py -- are
UNCHANGED and remain gated behind .ENTRY_CONFIRMED_<NAME>
(see ~/serenity-trader-stack/CLAUDE.md rule 1).

Design, each trading day:

1. MARKET-REGIME GATE (user: "如果盘前在跌，大盘在跌你可以不买股票" -- if
   pre-market/the broad market is down, you can skip buying). Checked once
   per day at the first tick: if SPY is below its previous close, skip all
   new entries for the day entirely (stay 100% in SGOV) -- don't force a
   trade into a weak tape.

2. STOCK SELECTION (once per day, cached): Exa search for real news
   catalysts (earnings beat, M&A, upgrades, product/partnership news) +
   a paid `claude -p` judgment call to shortlist up to 5 names with sizing.
   Deliberately NOT a momentum/top-gainers chase -- prefers a name with a
   real catalyst that ISN'T already vertical, per
   feedback_buy_dips_sell_strength.md ("抄底不是杀跌，卖高不是追涨"):
   buy a reasonable entry point, don't chase an already-extended move.

3. ENTRY: confirmed-uptrend only (Granville's Rules -- buy on the FIRST
   confirmed bullish tick off a base, not after 2+ consecutive up-moves,
   which risks buying an already-extended move). Same proven mechanism as
   plan_d_daytrade_20260715.py.

4. EXIT -- portfolio-level profit floor/ceiling band (see
   feedback_daily_profit_floor.md), NOT per-name noise-based selling (the
   2026-07-15 "exit on any decline tick" bug caused real churn losses --
   see PLAN_D.md's 2026-07-15 entry):
     a. Day P&L >= +2.0%: close EVERYTHING immediately, done for the day.
        User: "我觉得一天涨超过2%就可以收手了" -- lock in a big win, don't
        get greedy chasing more.
     b. Day P&L reached >= +0.1% at some point, then drops back to <=0.1%:
        close EVERYTHING, done for the day. Protects the user's stated
        floor ("每天至少要保证赚0.1%，这个底线要守住") without needing a
        per-tick stop-loss on any individual name.
     c. Below +0.1%, or before ever reaching it: HOLD, no exit at all
        (per "不要这样频繁买卖了" -- don't churn on noise).
     d. Mandatory close-out ~15min before market close regardless of P&L --
        the hard backstop, same as every prior script this week.

5. After close-out: sweep 100% of freed cash into SGOV, NO buffer (user:
   "不要留缓冲现金", 2026-07-15 night). One batched summary email at close
   (no per-trade emails, per earlier instruction).

State resets automatically at the start of each new trading day (this runs
indefinitely, unlike the one-off scripts it's built from).

HONEST LIMIT (see feedback_daily_profit_floor.md) -- state this in the
close-out report whenever it applies, don't hide it: the +0.1% floor is a
best-effort target, not a literal guarantee. If today's picks never clear
+0.1% at any point (e.g. the whole basket is red all day), Claude does
NOT escalate position size, add leverage, or hold a loser hoping for a
rebound to force the number -- that would be escalating risk under
pressure, against user_living_money_risk_posture's survival-first mandate.
A floor-miss is reported plainly in that day's summary.
"""
import sys, os, json, re, datetime, subprocess
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

MCPORTER = "/data/qbao775/miniconda3/bin/mcporter"
CLAUDE_BIN = "/home/qbao775/.local/bin/claude"

ENTRY_CONFIRM_TICKS = 1        # Granville's Rules -- buy on the first confirmed bullish tick
MAX_PICKS = 5
MAX_TOTAL_DEPLOY_PCT = 0.40    # cap total new-picks exposure -- this is a NEW autonomous
                               # stock-picking mechanism (different risk than SPY/QQQ market
                               # beta), stay meaningfully short of full deployment by default
FLOOR_PCT = 0.1                # protect this once reached (2026-07-15 night instruction)
CEILING_PCT = 2.0              # stop everything once reached (2026-07-15 night instruction)

STATE_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_state.json'


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
    log_entry = f"[{datetime.datetime.utcnow().strftime('%H:%M UTC')}] {text}"
    state.setdefault('action_log', []).append(log_entry)
    save_state(state)


def send_daily_summary(state, day_pl_pct, reason):
    actions = state.get('action_log', [])
    body = (f"今天(常态化自动日内交易)战况汇总 -- 收盘原因: {reason}\n"
            f"当日账户盈亏: {day_pl_pct:+.2f}%\n\n")
    if not actions:
        body += "今天没有交易(未找到合适标的,或大盘/盘前走弱选择空仓)。"
    else:
        body += "\n".join(actions)
    if day_pl_pct < FLOOR_PCT:
        body += (f"\n\n⚠️ 今天没有达到 {FLOOR_PCT}% 的底线目标。已如实汇报,"
                  f"不会为了凑数而加大仓位或硬扛亏损仓位赌反弹。")
    send_email(f"📊 每日自动日内交易 - 今日汇总 ({datetime.datetime.utcnow():%Y-%m-%d})", body)


def market_regime_ok(api):
    # user 2026-07-16: "如果盘前在跌，大盘在跌你可以不买股票" -- skip all new
    # entries for the day if the broad market (SPY) is below its prior close.
    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    try:
        r = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=15)
        snap = r.json()
        prev_close = snap['prevDailyBar']['c']
        last = snap.get('latestTrade', {}).get('p') or snap.get('minuteBar', {}).get('c')
        if not last:
            return True, None  # can't tell -- don't block on a data hiccup
        chg_pct = (last - prev_close) / prev_close * 100
        return chg_pct >= 0, chg_pct
    except Exception as e:
        log(f"  market regime check failed ({e}) -- not blocking on a data hiccup")
        return True, None


def pick_todays_stocks():
    log("  scanning for today's day-trade candidates...")
    search_snippets = []
    queries = [
        "stock market positive catalyst news today earnings beat upgrade",
        "stock M&A acquisition announcement today",
        "stock analyst upgrade price target raised today",
    ]
    for q in queries:
        try:
            env = dict(os.environ)
            env["PATH"] = "/data/qbao775/miniconda3/bin:" + env.get("PATH", "")
            r = subprocess.run([MCPORTER, "call", "exa.web_search_exa",
                                f"query={q}", "numResults=5"],
                               capture_output=True, text=True, timeout=60,
                               cwd="/data/qbao775/AlphaTrader", env=env)
            if r.returncode == 0:
                search_snippets.append(r.stdout[:3000])
            else:
                log(f"  search failed rc={r.returncode} for: {q}")
        except Exception as e:
            log(f"  search error for '{q}': {e}")

    if not search_snippets:
        log("  no search results at all -- skipping today's picks")
        return [], 0.0

    context = "\n\n---\n\n".join(search_snippets)
    prompt = (
        "你是短线交易研究员。基于下面的实时搜索结果,挑选今天(美股开盘)最多5只"
        "有真实利好消息支撑的股票(财报超预期、并购、评级上调、重大产品/合作公告等)。"
        "不要选纯粹因为'今天涨幅大'但找不到具体原因的票,也不要选已经拉得很高、"
        "追高风险大的票——优先选择消息真实、目前价格还没有过度透支的标的"
        "(抄底思路,不是追涨思路)。\n\n"
        f"搜索结果:\n{context}\n\n"
        "请严格按以下格式输出,每行一只股票,不要有其他文字或markdown:\n"
        "TICKER: 权重% 一句话理由\n"
        "例如:\nPYPL: 8% 财报超预期上调指引\n\n"
        "权重不要超过10%,最多5只。如果没有找到任何真正有说服力的标的,只输出: NONE"
    )
    try:
        result = subprocess.run(
            [CLAUDE_BIN, '-p', prompt, '--output-format', 'json'],
            capture_output=True, text=True, timeout=180,
            cwd='/data/qbao775/AlphaTrader'
        )
        if result.returncode != 0:
            log(f"  claude -p failed: {result.stderr[:200]}")
            return [], 0.0
        data = json.loads(result.stdout)
        cost = data.get('total_cost_usd', 0)
        answer = data.get('result', '')
        log(f"  claude -p pick cost: ${cost:.4f}")
        log(f"  raw picks:\n{answer}")
    except Exception as e:
        log(f"  claude -p exception: {e}")
        return [], 0.0

    picks = []
    for line in answer.splitlines():
        m = re.match(r'^\s*\$?([A-Z]{1,5})\s*:\s*(\d+(?:\.\d+)?)\s*%\s*(.*)$', line.strip())
        if m:
            sym, pct, reason = m.group(1), float(m.group(2)), m.group(3).strip()
            picks.append((sym, min(pct / 100, 0.10), reason))
    picks = picks[:MAX_PICKS]

    total_w = sum(p[1] for p in picks)
    if total_w > MAX_TOTAL_DEPLOY_PCT and total_w > 0:
        scale = MAX_TOTAL_DEPLOY_PCT / total_w
        picks = [(sym, w * scale, reason) for sym, w, reason in picks]

    return picks, cost


def enter(api, state):
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)

    for sym, w in state['weights'].items():
        sym_state = state['symbols'].setdefault(sym, {})
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
            log(f"  {sym}: px=${px:.2f} rise_streak={rise_streak}/{ENTRY_CONFIRM_TICKS} — not confirmed yet, not buying")
            save_state(state)
            continue

        notional = min(equity * w, bp - 20)
        qty = round(notional / px, 4)
        if qty <= 0:
            log(f"  {sym}: insufficient buying power — skipping")
            continue
        try:
            a = api.get_asset(sym)
            if not a.tradable:
                log(f"  {sym}: not tradable on Alpaca — skipping (bad pick from screen)")
                sym_state['entered'] = True  # don't keep retrying a dead pick all day
                save_state(state)
                continue
            o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
        except Exception as e:
            log(f"  {sym}: buy order failed ({e}) — will retry next tick")
            continue
        log(f"  ✓ BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]} (confirmed uptrend, {rise_streak} consecutive rises)")
        state['symbols'][sym] = {'entered': True}
        bp -= notional
        save_state(state)
        record_action(state, f"买入 {sym} {qty}股 @~${px:.2f} (确认{rise_streak}次连续上涨后进场) -- {state['reasons'].get(sym, '')}")


def liquidate_all(api, reason, state):
    positions = api.list_positions()
    for p in positions:
        try:
            o = api.submit_order(symbol=p.symbol, qty=p.qty, side='sell', type='market', time_in_force='day')
            plpc = float(p.unrealized_plpc) * 100
            log(f"  ✓ SOLD {p.symbol} qty={p.qty} order={o.id[:8]} — {reason}")
            record_action(state, f"卖出 {p.symbol} qty={p.qty} 盈亏{plpc:+.2f}% — {reason}")
        except Exception as e:
            log(f"  sell {p.symbol} failed: {e}")


def park_to_sgov():
    api = get_alpaca()
    import time
    time.sleep(8)  # let sell fills settle
    acc = api.get_account()
    cash = float(acc.cash)
    if cash < 5:
        return
    import requests
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/quotes/latest',
                      headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s})
    ask = r.json()['quote']['ap']
    limit_px = round(ask + 0.05, 2)
    qty = round(cash / limit_px, 4)  # no buffer -- user: "不要留缓冲现金"
    if qty <= 0:
        return
    ext = api.get_clock().is_open
    o = api.submit_order(symbol='SGOV', qty=qty, side='buy', type='limit',
                          limit_price=limit_px, time_in_force='day',
                          extended_hours=not ext)
    log(f"  parked ${cash:.2f} cash into {qty} SGOV @~${limit_px} order={o.id[:8]}")


def manage(api, state):
    acc = api.get_account()
    equity = float(acc.equity)
    day_start_equity = state['day_start_equity']
    day_pl_pct = (equity - day_start_equity) / day_start_equity * 100

    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60

    for sym in state['weights']:
        positions = {p.symbol: p for p in api.list_positions()}
        if sym in positions:
            p = positions[sym]
            log(f"  {sym}: qty={p.qty} plpc={float(p.unrealized_plpc)*100:+.2f}% (holding to close)")

    reason = None
    if day_pl_pct >= CEILING_PCT:
        reason = f"当日盈亏达到 {day_pl_pct:+.2f}%,触及 {CEILING_PCT}% 天花板,全部锁定离场"
    elif not state.get('floor_armed') and day_pl_pct >= FLOOR_PCT:
        state['floor_armed'] = True
        save_state(state)
        log(f"  floor armed: day P&L {day_pl_pct:+.2f}% cleared the {FLOOR_PCT}% floor")
    elif state.get('floor_armed') and day_pl_pct <= FLOOR_PCT:
        reason = f"当日盈亏从高于{FLOOR_PCT}%回落到 {day_pl_pct:+.2f}%,保护底线离场"
    elif mins_to_close <= 15:
        reason = f"距收盘不到15分钟 (当日盈亏 {day_pl_pct:+.2f}%),按规则强制平仓"

    if reason:
        liquidate_all(api, reason, state)
        state['done'] = True
        state['final_pl_pct'] = day_pl_pct
        save_state(state)
        log(f"today's auto day-trade wound down — {reason}")
        park_to_sgov()
        send_daily_summary(state, day_pl_pct, reason)
        return

    save_state(state)


def main():
    api = get_alpaca()
    clock = api.get_clock()
    today = clock.timestamp.strftime('%Y-%m-%d')
    state = load_state()

    if state.get('date') != today:
        state = {'date': today, 'symbols': {}, 'weights': {}, 'reasons': {},
                  'action_log': [], 'done': False}
        save_state(state)
        log(f"=== new trading day {today} -- state reset ===")

    if state.get('done'):
        return  # already wound down for today

    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    if 'day_start_equity' not in state:
        acc = api.get_account()
        state['day_start_equity'] = float(acc.equity)
        save_state(state)

    if not state.get('weights') and not state.get('skipped_regime'):
        ok, chg_pct = market_regime_ok(api)
        if not ok:
            log(f"  SPY pre-market/today {chg_pct:+.2f}% -- broad market weak, skipping today's entries entirely")
            state['skipped_regime'] = True
            state['done'] = True
            save_state(state)
            record_action(state, f"大盘走弱(SPY {chg_pct:+.2f}%),今天选择不建仓,继续持有美债")
            send_daily_summary(state, 0.0, "大盘/盘前走弱,今天选择空仓")
            return
        picks, cost = pick_todays_stocks()
        if not picks:
            log("  no qualifying picks today -- staying in cash/SGOV")
            state['done'] = True
            save_state(state)
            record_action(state, "今天没有找到有说服力的利好标的,继续持有美债")
            send_daily_summary(state, 0.0, "没有找到合适标的,今天选择空仓")
            return
        state['weights'] = {sym: w for sym, w, _ in picks}
        state['reasons'] = {sym: reason for sym, w, reason in picks}
        save_state(state)
        log(f"  today's picks: {state['weights']} (screen cost ${cost:.4f})")
        record_action(state, "今日选股: " + ", ".join(f"{s}({w*100:.0f}%,{state['reasons'][s]})" for s, w in state['weights'].items()))

    if not all(state['symbols'].get(sym, {}).get('entered') for sym in state['weights']):
        enter(api, state)
    else:
        manage(api, state)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION this tick:")
        log(traceback.format_exc())
