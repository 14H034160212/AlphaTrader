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
import sys, os, json, re, datetime, subprocess, requests
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

# 2026-07-16: user asked to paper-simulate before the market opens ("你可以
# 在模拟盘上先操盘模拟"). Rather than a separate reimplementation that could
# drift from the real logic, DRY_RUN reuses the EXACT same code path --
# picker, entry-confirm, floor/ceiling exits -- against a virtual cash/
# position ledger priced with real live market data, so tonight's rehearsal
# is testing the actual thing that goes live tomorrow, not an approximation.
DRY_RUN = os.environ.get('DOD_DRY_RUN') == '1'

ENTRY_CONFIRM_TICKS = 1        # Granville's Rules -- buy on the first confirmed bullish tick
MAX_PICKS = 6                  # 2026-07-16: raised 5->6 for more diversification -- more
                               # independent real-catalyst bets lowers the VARIANCE of the
                               # portfolio's day P&L around its mean, which raises the
                               # probability of clearing a small threshold like +0.1% without
                               # adding any leverage or risk per name (user: "优化系统提升
                               # 至少0.1%的概率" -- diversify/improve quality, don't escalate risk)
MAX_TOTAL_DEPLOY_PCT = 0.10    # 2026-07-16 (night): user asked to start the system's real,
                               # live first days at only 10-20% of total equity as a testing
                               # phase ("刚开始就用10%或者20%的总资金用来买美股测试这个系统")
                               # -- chose the more conservative end (10%) since this is a
                               # brand-new autonomous mechanism's first live run. Revisit
                               # raising this only after the system has proven itself over
                               # some real days -- don't creep it back up to 0.50 silently.
                               # The 6-name/50%-cap reasoning above still holds for WHY more
                               # names lowers variance; this just scales the whole thing down
                               # while trust is being built.
MAX_CHASE_GAP_PCT = 5.0        # 2026-07-16: skip a pick that's already up more than this much
                               # from its prior close before we even get to buy it -- a
                               # mechanical backstop for feedback_buy_dips_sell_strength.md
                               # ("卖高不是追涨") in case the LLM screen misses an extended move
SECOND_SCAN_AFTER_MIN = 90     # 2026-07-16: if the floor hasn't been touched after this long
                               # and real buying power remains uncommitted, run ONE more
                               # screen for fresh intraday catalysts rather than sitting on
                               # idle cash the rest of the day (still same quality bar --
                               # real catalyst + confirmed uptick, not chasing)
FLOOR_PCT = 0.1                # protect this once reached (2026-07-15 night instruction)
CEILING_PCT = 2.0              # stop everything once reached (2026-07-15 night instruction)
NO_PRICE_GIVEUP_TICKS = 15     # 2026-07-17: found via the dry-run -- ABB had no live price
                               # (yfinance: "possibly delisted") for the ENTIRE rest of a
                               # trading day, retried every tick with no cap. Give up after
                               # this many failed ticks instead of retrying forever.

STATE_FILE = ('/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_state.json' if DRY_RUN
              else '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_state.json')
HISTORY_FILE = ('/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_history.jsonl' if DRY_RUN
                else '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_history.jsonl')
# 2026-07-16: user asked every step to "串联" (chain together) and use past
# operating info, not treat each day as an isolated fresh start. Unlike
# STATE_FILE (which resets every trading day), this file is APPEND-ONLY
# across all days -- pick_todays_stocks() reads recent entries from it so
# the stock screen has real track-record context (which picks worked,
# which didn't, recent day P&L) instead of amnesia each morning.


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


def _alpaca_creds():
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    return k, s, u


def get_alpaca():
    import alpaca_trade_api as tradeapi
    k, s, u = _alpaca_creds()
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


def load_recent_history(n=10):
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        lines = open(HISTORY_FILE).read().splitlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]
    except Exception as e:
        log(f"  history read error: {e}")
        return []


def append_history(entry):
    try:
        with open(HISTORY_FILE, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log(f"  history write error: {e}")


def history_context_str():
    # Builds the "past operating info" summary the user asked to chain into
    # every day's decision, not just fresh news in isolation.
    hist = load_recent_history(10)
    if not hist:
        return ""
    lines = ["过去交易记录(供参考,避免重复踩坑/可以延续有效的方向):"]
    for h in hist:
        picks_str = ", ".join(f"{s}({w*100:.0f}%)" for s, w in h.get('weights', {}).items()) or "空仓"
        lines.append(f"- {h.get('date')}: {picks_str} -> 当日盈亏 {h.get('final_pl_pct', 0):+.2f}% ({h.get('reason', '')})")
    return "\n".join(lines) + "\n\n"


def already_held_elsewhere(api):
    # 2026-07-16: avoid the day-trade layer re-picking a name that's already
    # a dedicated long-term hold (SKHY/MU/META via skhy_position.py/
    # mu_reentry.py/meta_longhold.py) -- same separation-of-concerns
    # discipline as bull_day_trade_20260714.py, so a day-trade exit doesn't
    # get confused with / accidentally touch the long-term thesis position.
    LONG_TERM_NAMES = {'SKHY', 'MU', 'META'}
    try:
        held = {p.symbol for p in api.list_positions()}
    except Exception:
        held = set()
    return LONG_TERM_NAMES | (held - {'SGOV'})


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


def finalize_day(api, state, day_pl_pct, reason, do_liquidate=True):
    # Centralizes everything that must happen once a trading day is done,
    # from any of the 3 places a day can end (regime-skip, no-qualifying-
    # picks, or a real ceiling/floor/close-out trigger) -- keeps the
    # append-only HISTORY_FILE and the SGOV park-back consistent no matter
    # which exit path fired.
    if do_liquidate:
        liquidate_all(api, reason, state)
    state['done'] = True
    state['final_pl_pct'] = day_pl_pct
    save_state(state)
    log(f"today's auto day-trade wound down — {reason}")
    park_to_sgov()
    append_history({'date': state['date'], 'weights': state.get('weights', {}),
                     'reasons': state.get('reasons', {}), 'final_pl_pct': day_pl_pct,
                     'reason': reason})
    send_daily_summary(state, day_pl_pct, reason)


def market_regime_ok(api):
    # user 2026-07-16: "如果盘前在跌，大盘在跌你可以不买股票" -- skip all new
    # entries for the day if the broad market (SPY) is below its prior close.
    k, s, _ = _alpaca_creds()
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


def pick_todays_stocks(api, exclude=None):
    log("  scanning for today's day-trade candidates...")
    exclude = exclude or set()
    extra_context = history_context_str()
    if exclude:
        extra_context += (f"以下标的今天不要选(已经是长期持仓或当前已持有,"
                           f"避免和日内交易混淆): {', '.join(sorted(exclude))}\n\n")
    search_snippets = []
    queries = [
        "stock market positive catalyst news today earnings beat upgrade",
        "stock M&A acquisition announcement today",
        "stock analyst upgrade price target raised today",
        "biggest stock gainers today real news reason not hype",
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
        "你是短线交易研究员。基于下面的实时搜索结果,挑选今天(美股开盘)最多"
        f"{MAX_PICKS}只有真实利好消息支撑的股票(财报超预期、并购、评级上调、"
        "重大产品/合作公告等),尽量覆盖不同行业(分散,不要挤在同一个板块)。"
        "不要选纯粹因为'今天涨幅大'但找不到具体原因的票,也不要选已经拉得很高、"
        "追高风险大的票——优先选择消息真实、目前价格还没有过度透支的标的"
        "(抄底思路,不是追涨思路)。\n\n"
        "**宁缺毋滥**:如果只有2-3只真正有说服力,就只输出2-3只,不要为了凑数"
        "硬塞勉强的标的;如果一只都没有真正的信心,直接输出 NONE,今天空仓拿"
        "美债完全可以接受,不需要为了交易而交易。\n\n"
        f"{extra_context}"
        f"搜索结果:\n{context}\n\n"
        "请严格按以下格式输出,每行一只股票,不要有其他文字或markdown:\n"
        "TICKER: 权重% 一句话理由\n"
        "例如:\nPYPL: 8% 财报超预期上调指引\n\n"
        f"权重不要超过10%,最多{MAX_PICKS}只。如果没有找到任何真正有说服力的标的,只输出: NONE"
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
            if sym in exclude:
                log(f"  {sym}: excluded (already a long-term hold / already held) — skipping")
                continue
            picks.append((sym, min(pct / 100, 0.10), reason))
    picks = picks[:MAX_PICKS]

    # Mechanical "not already extended" backstop -- feedback_buy_dips_sell_strength.md
    # ("卖高不是追涨"): even with the prompt's own instruction, double-check each
    # pick isn't already up too much for the day before committing capital.
    checked = []
    _k, _s, _ = _alpaca_creds()
    for sym, w, reason in picks:
        try:
            r2 = requests.get(f'https://data.alpaca.markets/v2/stocks/{sym}/snapshot',
                               headers={'APCA-API-KEY-ID': _k,
                                        'APCA-API-SECRET-KEY': _s}, timeout=15)
            snap = r2.json()
            prev_close = snap['prevDailyBar']['c']
            last = snap.get('latestTrade', {}).get('p')
            if last and prev_close:
                gap_pct = (last - prev_close) / prev_close * 100
                if gap_pct > MAX_CHASE_GAP_PCT:
                    log(f"  {sym}: already up {gap_pct:.1f}% today (>{MAX_CHASE_GAP_PCT}%) — too extended, skipping (抄底不是追涨)")
                    continue
        except Exception as e:
            log(f"  {sym}: gap check failed ({e}) — not blocking on a data hiccup")
        checked.append((sym, w, reason))
    picks = checked

    total_w = sum(p[1] for p in picks)
    if total_w > MAX_TOTAL_DEPLOY_PCT and total_w > 0:
        scale = MAX_TOTAL_DEPLOY_PCT / total_w
        picks = [(sym, w * scale, reason) for sym, w, reason in picks]

    return picks, cost


def get_account_view(api, state):
    # Real (equity, buying_power) normally; a virtual ledger priced with
    # real live quotes in DRY_RUN, so the simulation and the live script
    # share every line of decision logic downstream of this call.
    if not DRY_RUN:
        acc = api.get_account()
        return float(acc.equity), float(acc.buying_power)
    if state.get('sim_cash') is None:
        acc = api.get_account()
        state['sim_cash'] = float(acc.equity)  # seed the virtual ledger once
        save_state(state)
    import market_data as md
    positions_value = 0.0
    for sym, pos in state.get('sim_positions', {}).items():
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else pos['entry_price']
        positions_value += pos['qty'] * px
    cash = state['sim_cash']
    return cash + positions_value, cash


def enter(api, state):
    import market_data as md
    equity, bp = get_account_view(api, state)

    for sym, w in state['weights'].items():
        sym_state = state['symbols'].setdefault(sym, {})
        if sym_state.get('entered'):
            continue

        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            sym_state['no_price_ticks'] = sym_state.get('no_price_ticks', 0) + 1
            if sym_state['no_price_ticks'] >= NO_PRICE_GIVEUP_TICKS:
                log(f"  {sym}: no live price for {sym_state['no_price_ticks']} ticks — giving up on this pick for today (bad data)")
                sym_state['entered'] = True  # stop retrying; never actually bought
                save_state(state)
                continue
            log(f"  {sym}: no live price yet ({sym_state['no_price_ticks']}/{NO_PRICE_GIVEUP_TICKS}) — will retry next tick")
            save_state(state)
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

        if DRY_RUN:
            state.setdefault('sim_positions', {})[sym] = {'qty': qty, 'entry_price': px}
            state['sim_cash'] = bp - notional
            log(f"  [DRY-RUN] ✓ BOUGHT {sym} qty={qty} @~${px:.2f} (confirmed uptrend, {rise_streak} consecutive rises)")
        else:
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
    if DRY_RUN:
        import market_data as md
        for sym, pos in list(state.get('sim_positions', {}).items()):
            q = md.get_stock_quote(sym)
            px = q['current'] if q and q.get('current') else pos['entry_price']
            plpc = (px - pos['entry_price']) / pos['entry_price'] * 100
            state['sim_cash'] = state.get('sim_cash', 0) + pos['qty'] * px
            log(f"  [DRY-RUN] ✓ SOLD {sym} qty={pos['qty']} @~${px:.2f} — {reason}")
            record_action(state, f"卖出 {sym} qty={pos['qty']} 盈亏{plpc:+.2f}% — {reason}")
        state['sim_positions'] = {}
        save_state(state)
        return
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
    if DRY_RUN:
        log("  [DRY-RUN] skipping SGOV park-back (simulation only, no real cash)")
        return
    api = get_alpaca()
    import time
    time.sleep(8)  # let sell fills settle
    acc = api.get_account()
    cash = float(acc.cash)
    if cash < 5:
        return
    k, s, _ = _alpaca_creds()
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
    equity, bp = get_account_view(api, state)
    day_start_equity = state['day_start_equity']
    day_pl_pct = (equity - day_start_equity) / day_start_equity * 100

    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60

    if DRY_RUN:
        import market_data as md
        for sym, pos in state.get('sim_positions', {}).items():
            q = md.get_stock_quote(sym)
            px = q['current'] if q and q.get('current') else pos['entry_price']
            plpc = (px - pos['entry_price']) / pos['entry_price'] * 100
            log(f"  [DRY-RUN] {sym}: qty={pos['qty']} plpc={plpc:+.2f}% (holding to close)")
    else:
        positions = {p.symbol: p for p in api.list_positions()}
        for sym in state['weights']:
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
        finalize_day(api, state, day_pl_pct, reason)
        return

    # 2026-07-16: second-chance re-scan -- user asked to raise the probability
    # of clearing the floor, and this is a legitimate way to do it (more
    # independent looks at the market, not more risk per look). If the floor
    # hasn't been touched after SECOND_SCAN_AFTER_MIN and there's still real
    # uncommitted buying power, take one more look for fresh catalysts.
    elapsed_min = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(state['day_start_time'])).total_seconds() / 60
    if (not state.get('floor_armed') and not state.get('second_scan_done')
            and elapsed_min >= SECOND_SCAN_AFTER_MIN and mins_to_close > 30):
        state['second_scan_done'] = True
        save_state(state)
        # 2026-07-17: BUG FOUND VIA THE DRY-RUN -- this used to scale new picks
        # against `bp/equity` (raw uncommitted CASH), which is nearly always
        # large since MAX_TOTAL_DEPLOY_PCT intentionally leaves most of the
        # account in cash/SGOV. That let the second scan add picks totalling
        # up to another full MAX_TOTAL_DEPLOY_PCT on TOP of what was already
        # deployed -- 2026-07-16 real example: RKT already used 5%, then the
        # second scan added UNH+ABB+CRWD (its own internal 10% cap) with
        # barely any additional scaling, pushing intended exposure to 12-15%,
        # over the 10% ceiling. The correct constraint is remaining ROOM
        # under the total cap, not remaining cash.
        current_total_w = sum(state['weights'].values())
        room = max(0.0, MAX_TOTAL_DEPLOY_PCT - current_total_w)
        if room > 0.01:  # only bother if there's meaningful cap room left
            log(f"  floor not yet touched after {elapsed_min:.0f}min -- running a second-chance scan (room={room*100:.1f}%)")
            exclude = already_held_elsewhere(api) | set(state['weights'].keys())
            picks, cost = pick_todays_stocks(api, exclude=exclude)
            if picks:
                total_new_w = sum(w for _, w, _ in picks)
                scale = min(1.0, room / total_new_w) if total_new_w else 0
                for sym, w, reason_txt in picks:
                    state['weights'][sym] = w * scale
                    state['reasons'][sym] = reason_txt
                save_state(state)
                log(f"  second-chance picks added: {[p[0] for p in picks]} (screen cost ${cost:.4f})")
                record_action(state, "补充选股(第二次扫描): " + ", ".join(f"{s}({w*scale*100:.0f}%,{r})" for s, w, r in picks))
            else:
                log("  second-chance scan found nothing new -- staying with current positions")

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
        state['day_start_time'] = datetime.datetime.utcnow().isoformat()
        save_state(state)

    if not state.get('weights') and not state.get('skipped_regime'):
        ok, chg_pct = market_regime_ok(api)
        if not ok:
            log(f"  SPY pre-market/today {chg_pct:+.2f}% -- broad market weak, skipping today's entries entirely")
            state['skipped_regime'] = True
            record_action(state, f"大盘走弱(SPY {chg_pct:+.2f}%),今天选择不建仓,继续持有美债")
            finalize_day(api, state, 0.0, "大盘/盘前走弱,今天选择空仓", do_liquidate=False)
            return
        exclude = already_held_elsewhere(api)
        picks, cost = pick_todays_stocks(api, exclude=exclude)
        if not picks:
            log("  no qualifying picks today -- staying in cash/SGOV")
            record_action(state, "今天没有找到有说服力的利好标的,继续持有美债")
            finalize_day(api, state, 0.0, "没有找到合适标的,今天选择空仓", do_liquidate=False)
            return
        state['weights'] = {sym: w for sym, w, _ in picks}
        state['reasons'] = {sym: reason for sym, w, reason in picks}
        save_state(state)
        log(f"  today's picks: {state['weights']} (screen cost ${cost:.4f})")
        record_action(state, "今日选股: " + ", ".join(f"{s}({w*100:.0f}%,{state['reasons'][s]})" for s, w in state['weights'].items()))

    # 2026-07-17: BUG FOUND VIA THE DRY-RUN (exactly why the user insisted on
    # testing this way first) -- this used to be if/else: as long as ANY
    # symbol had never entered, only enter() ran and manage() NEVER did, for
    # the rest of the day. ABB had no live price all day (yfinance: "possibly
    # delisted") and got retried every single tick from 15:01 to market
    # close at 20:00 UTC -- meanwhile RKT/UNH/CRWD were already bought and
    # manage() never got called once, so the mandatory close-out, the +2%
    # ceiling, and the +0.1% floor protection NEVER RAN for the whole
    # afternoon. In live mode this would have held real positions overnight
    # with zero exit monitoring -- exactly the risk the close-out exists to
    # prevent. Fix: always attempt entries for whatever hasn't entered yet,
    # AND always manage whatever IS currently held, every single tick --
    # these are not mutually exclusive phases.
    enter(api, state)
    if any(state['symbols'].get(sym, {}).get('entered') for sym in state['weights']):
        manage(api, state)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION this tick:")
        log(traceback.format_exc())
