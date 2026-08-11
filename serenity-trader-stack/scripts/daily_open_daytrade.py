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
# 2026-07-28: user's standing principle, stated after the live TEL position failed to
# follow the paper account's AI exit -- **"实盘的操作应该和模拟盘一致"** (the live
# account's operations must be consistent with the paper account). With this flag on,
# every sim trade the paper loop executes is immediately mirrored on the REAL account:
# sim buy -> real buy (same weight of real equity, funded by trimming SGOV if needed);
# sim sell -> real sell of the same symbol; after mirrored sells, real proceeds are
# parked into SGOV per the standing rule. A mirror failure never breaks the paper loop
# (each mirror op is individually try/except'd) -- divergence is logged loudly instead.
MIRROR_TO_LIVE = os.environ.get('DOD_MIRROR_LIVE') == '1'

ENTRY_CONFIRM_TICKS = 1        # Granville's Rules -- buy on the first confirmed bullish tick
# 2026-07-22: user asked to clear out every numeric rule constant and let the AI decide
# everything ("请你把系统所有这些规则的限定都清除，以后全部让ai自己决定，我相信ai的判断",
# then "实盘也删除" / "全部去掉" / "让ai自己判断" for the exit side too). Claude pushed back
# on flipping the LIVE CRON on with zero track record for the new AI-judged-exit logic;
# user agreed to keep the live cron paused until the paper side has real results
# ("等模拟盘有结果再放实盘") -- see project_management_mandate memory. This cleanup pass
# removes the now-dead LIVE/SIM constant split from the CODE (the numbers themselves,
# not the live cron switch) per the user's explicit clarification that this is a code
# cleanup, not turning live trading back on: "只是把代码里那些旧的数字常量清理干净,但
# 实盘的cron还是保持暂停、不会真的开始拿真钱交易". IMPORTANT: whenever the live cron is
# re-enabled in the future, it will run with these SAME unconstrained numbers -- there is
# no longer a separate conservative live path in this file. If a future session is asked
# to re-enable live trading, flag this explicitly before doing so.
MAX_PICKS = 20                 # generous practical bound, not a judgment limit -- the
                               # LLM's own "宁缺毋滥" prompt instruction governs how many
                               # it actually picks
MAX_PICK_WEIGHT = 1.0          # no per-name cap -- the AI's own requested conviction % is
                               # trusted directly
MAX_TOTAL_DEPLOY_PCT = 1.0     # no total-exposure cap
MAX_SINGLE_NAME_CONCENTRATION_PCT = 0.35   # 2026-08-06: hard ceiling for ROTATE_TO --
                                            # found via self-review that repeated sound-
                                            # looking rotations (5x in ~2h) consolidated a
                                            # 9-name book into 2 names at 91.6% of the
                                            # account with no ceiling at all. "Concentrate
                                            # into a proven winner" was never meant to mean
                                            # "up to effectively 100%" -- this caps any
                                            # single ROTATE_TO target's resulting weight.
MAX_ROTATIONS_PER_DAY = 2      # independent guard on the CASCADE itself -- each
                                # individual rotation can look sound while the
                                # cumulative effect (this many in one session) wasn't
                                # authorized at all
MAX_CHASE_GAP_PCT = 1000.0     # effectively disabled -- the picker's own prompt-level
                               # "don't chase an extended move" instruction is what's left
SECOND_SCAN_AFTER_MIN = 90     # if the day's P&L hasn't cleared FLOOR_PCT after this long
                               # and real buying power remains uncommitted, run ONE more
                               # screen for fresh intraday catalysts rather than sitting on
                               # idle cash the rest of the day (still same quality bar --
                               # real catalyst + confirmed uptick, not chasing)
MIN_DEPLOYED_PCT_BEFORE_RESCAN = 0.30  # 2026-07-23: user asked "你的持仓还需要调整吗" after
                               # the widened search found 6 good diversified picks while
                               # the account sat >85% idle in cash/SGOV with only one
                               # position (already profitable, so the P&L-based rescan
                               # trigger below never fired). Added a SECOND, independent
                               # reason to rescan: if still under this much total exposure
                               # after SECOND_SCAN_AFTER_MIN, look for more regardless of
                               # current P&L -- idle capital + good opportunities elsewhere
                               # is its own reason to diversify, not just underperformance.
FLOOR_PCT = 0.1                # used only as the second-chance-scan trigger threshold now
                               # (see manage()) -- no longer a hard sell trigger anywhere;
                               # ai_judge_positions() decides all holds/sells/exits
CEILING_PCT = 2.0              # UNUSED -- kept only so old state files referencing it (if
                               # any) don't error; exits are entirely AI-judged now
POSITION_JUDGE_COOLDOWN_MIN = 20   # don't re-ask more often than this (cost control),
                                    # except when close is near -- then ask every tick
                                    # until it gives an unambiguous HOLD_OVERNIGHT/SELL_ALL
NO_PRICE_GIVEUP_TICKS = 15     # 2026-07-17: found via the dry-run -- ABB had no live price
                               # (yfinance: "possibly delisted") for the ENTIRE rest of a
                               # trading day, retried every tick with no cap. Give up after
                               # this many failed ticks instead of retrying forever.

NEW_ENTRIES_PAUSED_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_NEW_ENTRIES_PAUSED'
# 2026-07-23: user, right as the idle-capital rescan fix above was about to fire for the
# first time -- "我建议不要再加仓位了，因为代码还在更新" + "尽量稳一些" (don't add more
# positions while the code is still being actively changed, stay stable). Rather than
# reverting the fix (which is correct and should stay), this marker file (created
# out-of-band, not by this script) gates NEW position additions only (both the initial
# daily pick and the second-chance rescan) -- existing positions (e.g. TEL) still get
# managed normally by ai_judge_positions(). Delete this file only once the user says the
# code has stabilized and new entries can resume -- don't remove it on your own judgment.

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


LESSONS_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_lessons.jsonl'
WATCHBACK_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_watchback.jsonl'
WATCHBACK_LOOKBACK_DAYS = 14

# 2026-08-06: user -- "时刻要和大盘收益比较". Benchmark anchors for cumulative
# comparison: the system's live inception (2026-07-16, account $61,016.51) and
# SPY's prior close ($754.81 on 2026-07-15), so every daily email can state
# cumulative account return vs cumulative SPY over the same period.
INCEPTION_EQUITY = 61016.51
INCEPTION_SPY_CLOSE = 754.81


def spy_day_change():
    try:
        k, s, _ = _alpaca_creds()
        r = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
        snap = r.json()
        prev = snap['prevDailyBar']['c']
        last = snap.get('latestTrade', {}).get('p')
        if prev and last:
            return round((last - prev) / prev * 100, 2), last
    except Exception:
        pass
    return None, None


def history_context_str():
    # Builds the "past operating info" summary the user asked to chain into
    # every day's decision, not just fresh news in isolation.
    hist = load_recent_history(10)
    parts = []
    if hist:
        lines = ["过去交易记录(供参考,避免重复踩坑/可以延续有效的方向):"]
        for h in hist:
            picks_str = ", ".join(f"{s}({w*100:.0f}%)" for s, w in h.get('weights', {}).items()) or "空仓"
            spy = h.get('spy_pct')
            spy_str = f" vs SPY {spy:+.2f}%" if isinstance(spy, (int, float)) else ""
            lines.append(f"- {h.get('date')}: {picks_str} -> 当日盈亏 {h.get('final_pl_pct', 0):+.2f}%{spy_str} ({h.get('reason', '')})")
        parts.append("\n".join(lines))
    # 2026-08-05: the automated post-close retro (daily_retro.py) writes
    # concrete lessons; feed the recent ones into every morning's pick so
    # yesterday's mistake is part of tomorrow's decision context (user: "你
    # 需要有这样的反思能力...不要等我给你授权").
    lc = lessons_context_str()
    if lc:
        parts.append(lc)
    # 2026-08-11: user noticed FTK kept rallying on real fundamentals after
    # we sold it (for concentration, not thesis reasons) and never came back
    # into consideration -- generic queries only catch FRESH same-day news,
    # so a multi-day-old still-valid catalyst falls off the radar the moment
    # a name is sold. Explicitly re-surface recent exits' price action.
    wb = watch_back_context_str()
    if wb:
        parts.append(wb)
    return ("\n\n".join(parts) + "\n\n") if parts else ""


def lessons_context_str(max_lines=10):
    # 2026-08-05: shared by BOTH the morning picker and the intraday position
    # judge (user: "好的和坏的投资都要吸取经验"). Before this, lessons only
    # reached the picker -- the judge kept holding COLM even though the ledger
    # already said its one-off-refund catalyst was invalid, because it never
    # saw the ledger.
    try:
        if not os.path.exists(LESSONS_FILE):
            return ""
        lesson_lines = []
        for line in open(LESSONS_FILE).read().splitlines()[-10:]:
            try:
                e = json.loads(line)
                for les in e.get('lessons', []):
                    lesson_lines.append(f"- ({e.get('date')}) {les}")
            except Exception:
                continue
        if not lesson_lines:
            return ""
        return ("历史复盘教训(好的经验要复制,坏的错误不要重复;逐条对照当前决定):\n"
                + "\n".join(lesson_lines[-max_lines:]))
    except Exception:
        return ""


def record_watch_back(symbol, exit_price, reason):
    # 2026-08-11: user noticed FTK (sold 2026-08-07 for CONCENTRATION reasons,
    # thesis explicitly stated as intact) kept rallying on real fundamentals
    # (Q2 beat + guidance raise) and never resurfaced in any later day's
    # picks -- the generic search queries are tuned for FRESH same-day news,
    # so a name whose catalyst is a few days old with no NEW headline that
    # day simply falls off the radar entirely once sold, regardless of
    # whether the reason for selling had anything to do with the thesis.
    # Log every sell here (thesis-broken exits included -- cheap to check,
    # and being wrong about "still worth watching" costs nothing but one
    # price lookup) so the picker can explicitly re-examine it later instead
    # of relying on luck to re-trigger a generic query.
    try:
        entry = {'date': datetime.date.today().isoformat(), 'symbol': symbol,
                  'exit_price': exit_price, 'reason': reason}
        with open(WATCHBACK_FILE, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def watch_back_context_str():
    # Companion to record_watch_back(): re-checks recently-exited names'
    # price action so the picker can explicitly decide whether a still-valid
    # thesis deserves re-entry, rather than needing the name to independently
    # re-trigger a generic search query.
    if not os.path.exists(WATCHBACK_FILE):
        return ""
    try:
        import market_data as md
        cutoff = datetime.date.today() - datetime.timedelta(days=WATCHBACK_LOOKBACK_DAYS)
        seen = set()
        lines = []
        for line in reversed(open(WATCHBACK_FILE).read().splitlines()):
            try:
                e = json.loads(line)
            except Exception:
                continue
            sym = e.get('symbol')
            if not sym or sym in seen:
                continue
            try:
                d = datetime.date.fromisoformat(e.get('date', ''))
            except Exception:
                continue
            if d < cutoff:
                continue
            seen.add(sym)
            q = md.get_stock_quote(sym)
            now_px = q['current'] if q and q.get('current') else None
            exit_px = e.get('exit_price')
            if now_px and exit_px:
                chg = (now_px - exit_px) / exit_px * 100
                lines.append(f"- {sym}: {e['date']}卖出@${exit_px:.2f} -> 现价${now_px:.2f} ({chg:+.1f}%), "
                             f"当时卖出理由: {e.get('reason', '')[:120]}")
        if not lines:
            return ""
        return ("最近卖出过的标的近况(不代表建议买回,只是提醒你自己判断是否值得重新考虑,"
                "尤其如果卖出理由是仓位/集中度而非论文本身破裂):\n" + "\n".join(lines[:8]))
    except Exception:
        return ""


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
    # benchmark comparison, daily AND cumulative ("时刻要和大盘收益比较")
    bench = ""
    spy_chg, spy_last = spy_day_change()
    if spy_chg is not None:
        rel = day_pl_pct - spy_chg
        bench = f"当日大盘SPY: {spy_chg:+.2f}% | 相对大盘: {rel:+.2f}pp ({'跑赢' if rel >= 0 else '跑输'})\n"
        try:
            api = get_alpaca()
            eq = float(api.get_account().equity)
            cum_us = (eq - INCEPTION_EQUITY) / INCEPTION_EQUITY * 100
            cum_spy = (spy_last - INCEPTION_SPY_CLOSE) / INCEPTION_SPY_CLOSE * 100
            bench += (f"累计(自2026-07-16): 账户 {cum_us:+.2f}% vs SPY {cum_spy:+.2f}% "
                      f"({'跑赢' if cum_us >= cum_spy else '跑输'} {abs(cum_us-cum_spy):.2f}pp)\n")
        except Exception:
            pass
    body = (f"今天(常态化自动日内交易)战况汇总 -- 收盘原因: {reason}\n"
            f"当日账户盈亏: {day_pl_pct:+.2f}%\n{bench}\n")
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
    live_sold = 0
    if do_liquidate:
        live_sold = liquidate_all(api, reason, state) or 0
    state['done'] = True
    state['final_pl_pct'] = day_pl_pct
    save_state(state)
    log(f"today's auto day-trade wound down — {reason}")
    # 2026-07-31: a park_to_sgov crash (insufficient buying power -- sell fills
    # hadn't settled yet) used to abort the rest of finalize_day, silently
    # skipping the history append AND the daily summary email. Never let the
    # park kill the bookkeeping: it gets its own try/except, and the history/
    # email always run.
    try:
        park_to_sgov(mirror_live=(DRY_RUN and MIRROR_TO_LIVE and live_sold > 0))
    except Exception as e:
        log(f"  park_to_sgov FAILED ({e}) -- cash left unparked, continuing with bookkeeping")
        record_action(state, f"⚠️ 收盘后停美债失败({e}),现金暂未停放,需要人工处理")
    append_history({'date': state['date'], 'weights': state.get('weights', {}),
                     'reasons': state.get('reasons', {}), 'final_pl_pct': day_pl_pct,
                     'spy_pct': spy_day_change()[0], 'reason': reason})
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


def pick_todays_stocks(api, exclude=None, extra_note=""):
    log("  scanning for today's day-trade candidates...")
    exclude = exclude or set()
    extra_context = history_context_str() + extra_note
    if exclude:
        extra_context += (f"以下标的今天不要选(已经是长期持仓或当前已持有,"
                           f"避免和日内交易混淆): {', '.join(sorted(exclude))}\n\n")
    # 2026-07-23: user said the search scope was too narrow ("请你搜索的更广") --
    # widened from 4 queries covering only earnings/M&A/upgrades/generic gainers
    # to a much broader sweep across sectors and catalyst types, plus a real
    # market-wide screener (not just text search) for actual price-mover
    # coverage the queries alone might miss.
    search_snippets = []
    queries = [
        "stock market positive catalyst news today earnings beat upgrade",
        "stock M&A acquisition announcement today",
        "stock analyst upgrade price target raised today",
        "biggest stock gainers today real news reason not hype",
        "biotech FDA approval drug trial results today",
        "semiconductor AI chip company news today",
        "energy oil gas utility company news today",
        "financial services bank insurance earnings news today",
        "government contract award defense aerospace news today",
        "retail consumer company earnings guidance news today",
        "small cap stock breakout news today",
        "insider buying stock news today",
        # 2026-08-07: added after missing the AAOI/COHR/LITE optical-interconnect
        # rally -- the underlying signal (TrendForce InP shortage report, 8/6,
        # hours before market open) was a real sourced supply-chain bottleneck,
        # not "sector sentiment", but none of the queries above would ever
        # surface a TrendForce/DigiTimes-style industry data-point. This is
        # exactly the class of signal Serenity's chokepoint lens looks for.
        "TrendForce DigiTimes semiconductor component shortage supply chain report",
        "AI data center hyperscaler capex optical networking supply constraint bottleneck",
        # 2026-08-08: user asked to broaden scope further after a semiconductor +
        # optical + SpaceX(SPCX) + gold/silver rally the picker's query set had
        # no dedicated coverage for -- "我觉得你选股的能力很强，但是搜索的范围
        # 还不太广". Precious metals wasn't a previously-declared focus theme,
        # so this is exploratory breadth, not a priority weight like semis/AI.
        "gold silver precious metals mining stock news today",
        "space launch satellite company news today",
    ]
    # 2026-07-31: found post-reboot that firing all queries back-to-back trips the
    # Exa endpoint's rate limit (HTTP 429) and ALL searches fail -- space them out
    # and retry once with a longer pause. The picker degraded gracefully that day
    # (market-screener supplement + the LLM's own knowledge still produced a sound
    # NONE call), but at 4x the claude -p cost -- fix the input pipe, not just rely
    # on the fallback.
    import time as _time
    for qi, q in enumerate(queries):
        for attempt in (1, 2):
            try:
                env = dict(os.environ)
                env["PATH"] = "/data/qbao775/miniconda3/bin:" + env.get("PATH", "")
                r = subprocess.run([MCPORTER, "call", "exa.web_search_exa",
                                    f"query={q}", "numResults=5"],
                                   capture_output=True, text=True, timeout=60,
                                   cwd="/data/qbao775/AlphaTrader", env=env)
                if r.returncode == 0:
                    search_snippets.append(r.stdout[:3000])
                    break
                rate_limited = '429' in (r.stderr or '')
                log(f"  search failed rc={r.returncode}{' (rate-limited)' if rate_limited else ''} "
                    f"attempt {attempt} for: {q}")
                if attempt == 1:
                    _time.sleep(12 if rate_limited else 3)
            except Exception as e:
                log(f"  search error for '{q}': {e}")
                break
        _time.sleep(4)  # pace queries so we don't trip the rate limit again

    # Real market-wide screener supplement (actual price/volume movers, not text
    # search) -- labeled clearly so the AI verifies the REASON itself rather than
    # chasing a bare mover, per feedback_buy_dips_sell_strength.md.
    try:
        import yfinance as yf
        gainers = yf.screen('day_gainers', count=25)
        rows = gainers.get('quotes', [])
        mover_lines = []
        movers_syms = []
        for row in rows[:25]:
            sym = row.get('symbol')
            chg = row.get('regularMarketChangePercent')
            price = row.get('regularMarketPrice')
            vol = row.get('regularMarketVolume')
            if sym and chg is not None and price and price >= 10 and vol and vol >= 300_000:
                mover_lines.append(f"{sym}: {chg:+.1f}% (现价${price:.2f})")
                movers_syms.append(sym)
        if mover_lines:
            search_snippets.append("实时全市场涨幅榜(仅供参考,自己判断消息是否真实,"
                                    "不要单纯因为涨幅大就选):\n" + "\n".join(mover_lines))

        # 2026-08-07: the generic Exa text search repeatedly failed to confirm
        # a specific catalyst for names that WERE on this gainers list (INSM,
        # WPP, PAYC... rejected daily as "涨幅榜但无具体消息"), so real picks
        # were being missed for lack of a precise per-symbol source. Alpaca's
        # own news API is structured and symbol-tagged -- a much sharper tool
        # for "does THIS ticker have an actual headline" than freeform search.
        # 2026-08-10: reuse the SAME price/volume-filtered symbol list as
        # mover_lines above -- pulling news for the raw unfiltered rows was
        # reintroducing the thin/penny-stock noise that filter exists to cut.
        if movers_syms:
            try:
                k, s, _ = _alpaca_creds()
                h = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}
                r = requests.get('https://data.alpaca.markets/v1beta1/news',
                                  params={'symbols': ','.join(movers_syms), 'limit': 40},
                                  headers=h, timeout=15)
                articles = r.json().get('news', []) if r.status_code == 200 else []
                if articles:
                    news_lines = [f"{a['created_at'][:16]} | {','.join(a['symbols'])} | {a['headline']}"
                                  for a in articles]
                    search_snippets.append(
                        "涨幅榜标的的结构化新闻标题(Alpaca News,按symbol精确匹配,"
                        "比通用搜索更能确认具体某只股票是否真的有消息):\n"
                        + "\n".join(news_lines))
            except Exception as e:
                log(f"  alpaca news lookup error: {e}")
    except Exception as e:
        log(f"  market screener error: {e}")

    if not search_snippets:
        log("  no search results at all -- skipping today's picks")
        return [], 0.0

    weight_rule = (f"每只权重不要超过{MAX_PICK_WEIGHT*100:.0f}%,最多{MAX_PICKS}只。"
                   if MAX_PICK_WEIGHT < 1.0 else
                   "权重按你自己的把握程度定,没有固定上限——但仍然要体现真实的相对置信度"
                   f"(比如高确信可以明显重一些),不是无脑平均分配。最多{MAX_PICKS}只。")
    context = "\n\n---\n\n".join(search_snippets)
    prompt = (
        "你是短线交易研究员。基于下面的实时搜索结果,挑选今天(美股开盘)最多"
        f"{MAX_PICKS}只有真实利好消息支撑的股票(财报超预期、并购、评级上调、"
        "重大产品/合作公告等),尽量覆盖不同行业(分散,不要挤在同一个板块)。"
        "权重分配要同时考虑两个维度:催化剂的确定性 和 股价的预期弹性——"
        "大盘股/防御性板块(如大型医药)即使消息扎实,单日波动空间也有限,"
        "不要因为'最稳'就给最大权重,但这是一个降低权重的理由,不是排除的理由——"
        "如果多只大盘龙头股同时因为同一个真实、可验证的行业级消息大涨"
        "(比如整条产业链的供应链数据或政策信息),这种广度本身就是需要"
        "全面判断的强信号,不要仅仅因为市值大就直接不选,可以给中等偏低"
        "的权重参与,而不是完全排除;弹性大、催化硬的中小盘才配得上高权重。"
        "不要选纯粹因为'今天涨幅大'但找不到具体原因的票,也不要选已经拉得很高、"
        "追高风险大的票——优先选择消息真实、目前价格还没有过度透支的标的"
        "(抄底思路,不是追涨思路)。\n"
        "每只候选必须评估**利好的持续性**:这条消息是'一次性兑现'(评级上调、一次性"
        "会计收益——消息当天基本走完),还是'数日发酵'(财报超预期+上调指引、并购进程),"
        "还是'数周以上的持续兑现'(独占长期合同、FDA批准打开新市场)?持续性越长越值得"
        "买、越配高权重;一次性利好除非弹性极大否则宁可不碰。同时评估**股价对这条消息"
        "的敏感度**:小盘+独占催化=高敏感(FTK型),大盘+常规利好=低敏感(NVS型),只有"
        "高敏感标的配高权重。\n\n"
        "**宁缺毋滥**:如果只有2-3只真正有说服力,就只输出2-3只,不要为了凑数"
        "硬塞勉强的标的;如果一只都没有真正的信心,直接输出 NONE,今天空仓拿"
        "美债完全可以接受,不需要为了交易而交易。\n\n"
        f"{extra_context}"
        f"搜索结果:\n{context}\n\n"
        "请严格按以下格式输出,不要有markdown:\n"
        "先按每行一只股票列出你选中的:\n"
        "TICKER: 权重% [持续性:一次性/数日/数周+] 一句话理由\n"
        "例如:\nPYPL: 8% [持续性:数日] 财报超预期上调指引\n\n"
        f"{weight_rule}如果没有找到任何真正有说服力的标的,只输出: NONE\n\n"
        "选完之后另起一行,以 REJECTED: 开头,简要列出你在搜索结果里看到但没有选的"
        "其他标的和理由(比如'追高风险大'、'消息不够具体'、'已经在长期持仓里'等),"
        "方便事后复盘对比。如果搜索结果里没有其他候选,写 REJECTED: 无。"
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
            picks.append((sym, min(pct / 100, MAX_PICK_WEIGHT), reason))
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
        total_w = MAX_TOTAL_DEPLOY_PCT

    # 2026-08-07: "把标普500作为一个baseline" -- SPY owns whatever capital
    # individual picks didn't explicitly carve out, as a guaranteed backstop
    # (not dependent on the LLM remembering to compute the remainder itself).
    remainder = MAX_TOTAL_DEPLOY_PCT - total_w
    if remainder > 0.01:
        existing_spy = next((i for i, p in enumerate(picks) if p[0] == 'SPY'), None)
        if existing_spy is not None:
            sym, w, reason = picks[existing_spy]
            picks[existing_spy] = (sym, w + remainder, reason)
        else:
            picks.append(('SPY', remainder, '基准仓位(个股未占满的资金默认留在大盘)'))

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

        desired_notional = equity * w
        # 2026-08-10: user pointed out "SPY里的钱可以动的" -- SPY is the
        # flexible default/baseline (individual picks carve capital OUT of
        # it), not a locked core holding, but this function had no mechanism
        # to actually reclaim SPY capital for a fresh, LLM-approved,
        # confirmed-uptick pick. Real incident: a second-chance scan found 7
        # real catalyst picks (MLTX/DDOG/RKLB/EMBJ/GRAL/TEM/CXW, 69% intended
        # weight) but EVERY one failed all afternoon on "insufficient buying
        # power" -- SPY had already absorbed all available cash from an
        # earlier trade that same morning, and nothing here would trim it
        # back even though SPY existing for exactly this purpose. Trim SPY
        # (only SPY -- never a name from LONG_TERM_NAMES_NEVER_TOUCH) to
        # cover the shortfall when this pick needs more than raw cash covers.
        if sym != 'SPY' and bp - 20 < desired_notional:
            shortfall = desired_notional - (bp - 20)
            spy_pos = state.get('sim_positions', {}).get('SPY') if DRY_RUN else None
            spy_live_qty = None
            if not DRY_RUN:
                try:
                    spy_live_qty = float(api.get_position('SPY').qty)
                except Exception:
                    spy_live_qty = None
            have_spy = spy_pos is not None if DRY_RUN else (spy_live_qty is not None and spy_live_qty > 0)
            if have_spy:
                spy_q = md.get_stock_quote('SPY')
                spy_px = spy_q['current'] if spy_q and spy_q.get('current') else None
                if spy_px:
                    spy_value = (spy_pos['qty'] * spy_px) if DRY_RUN else (spy_live_qty * spy_px)
                    trim_value = min(shortfall, spy_value)
                    trim_qty = round(trim_value / spy_px, 4)
                    if trim_qty > 0:
                        log(f"  {sym}: raising ${trim_value:.2f} by trimming SPY (baseline capital reclaimed for a confirmed pick)")
                        if DRY_RUN:
                            spy_pos['qty'] = round(spy_pos['qty'] - trim_qty, 4)
                            if spy_pos['qty'] <= 0:
                                state['sim_positions'].pop('SPY', None)
                            bp += trim_value
                            state['sim_cash'] = bp
                            record_action(state, f"卖出SPY {trim_qty}股 @~${spy_px:.2f} 腾出资金给 {sym}")
                            if MIRROR_TO_LIVE:
                                try:
                                    o = api.submit_order(symbol='SPY', qty=trim_qty, side='sell', type='market', time_in_force='day')
                                    log(f"  [LIVE-MIRROR] ✓ trimmed SPY qty={trim_qty} order={o.id[:8]}")
                                except Exception as e:
                                    log(f"  [LIVE-MIRROR] SPY trim for {sym} FAILED -- live has DIVERGED from paper: {e}")
                        else:
                            try:
                                api.submit_order(symbol='SPY', qty=trim_qty, side='sell', type='market', time_in_force='day')
                                bp += trim_value
                            except Exception as e:
                                log(f"  {sym}: SPY trim failed ({e}) — proceeding with whatever cash is available")

        notional = min(desired_notional, bp - 20)
        qty = round(notional / px, 4)
        if qty <= 0:
            log(f"  {sym}: insufficient buying power — skipping")
            continue

        if DRY_RUN:
            # 2026-08-10: REAL INCIDENT -- this used to unconditionally
            # overwrite sim_positions[sym], silently erasing a carried-over
            # hold_overnight position's tracked share count. SPY specifically
            # hits this every day it's both (a) carried over from yesterday
            # AND (b) re-appears in today's fresh `weights` via the
            # remainder-fill baseline logic -- day-rollover resets
            # state['symbols'] (the entered-flag tracker) but correctly
            # preserves sim_positions, so `enter()` treats SPY as brand new
            # and wiped a 58.5535-share position down to just today's
            # incremental buy, making the paper ledger's tracked equity
            # collapse from ~$64k to ~$18.5k and the displayed day P&L show
            # a nonsensical -71%. Merge into any existing position instead.
            sp = state.setdefault('sim_positions', {})
            if sym in sp:
                old = sp[sym]
                new_qty = old['qty'] + qty
                sp[sym] = {'qty': new_qty,
                           'entry_price': round((old['qty'] * old['entry_price'] + qty * px) / new_qty, 4)}
            else:
                sp[sym] = {'qty': qty, 'entry_price': px}
            state['sim_cash'] = bp - notional
            log(f"  [DRY-RUN] ✓ BOUGHT {sym} qty={qty} @~${px:.2f} (confirmed uptrend, {rise_streak} consecutive rises)")
            if MIRROR_TO_LIVE:
                try:
                    live_qty = mirror_live_buy(api, sym, w, px)
                    if live_qty:
                        record_action(state, f"[实盘同步] 买入 {sym} {live_qty}股 @~${px:.2f}")
                except Exception as e:
                    log(f"  [LIVE-MIRROR] buy {sym} FAILED -- live has DIVERGED from paper: {e}")
                    record_action(state, f"[实盘同步失败] {sym} 实盘买入失败,实盘与模拟盘出现偏差: {e}")
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


def mirror_live_buy(api, sym, w, px):
    # LIVE mirror of a sim buy: same weight applied to REAL equity, funded by
    # trimming SGOV if free buying power is short ("实盘的操作应该和模拟盘一致").
    acct = api.get_account()
    equity = float(acct.equity)
    notional = equity * w
    bp = float(acct.buying_power)
    if bp < notional + 5:
        # Proceeds from a just-executed sell may simply still be settling --
        # poll briefly before deciding we actually need to trim SGOV.
        import time as _t
        for _ in range(8):
            _t.sleep(3)
            bp = float(api.get_account().buying_power)
            if bp >= notional + 5:
                break
    if bp < notional + 5:
        shortfall = notional + 5 - bp
        k, s, _ = _alpaca_creds()
        r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/trades/latest',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=15)
        sgov_px = r.json()['trade']['p']
        try:
            held_sgov = float(api.get_position('SGOV').qty)
        except Exception:
            held_sgov = 0.0
        sq = min(round(shortfall / sgov_px + 0.01, 4), held_sgov)
        if sq > 0:
            so = api.submit_order(symbol='SGOV', qty=sq, side='sell', type='market', time_in_force='day')
            log(f"  [LIVE-MIRROR] sold {sq} SGOV to fund {sym} buy")
            # 2026-08-03: a fixed 6s sleep raced fill settlement and FIVE mirror
            # buys died with "insufficient buying power" in one session (BABA/
            # ABT/DBD/FTK/SUPN), leaving live badly diverged from paper. Wait
            # for the funding sale to actually FILL, then for buying power to
            # actually reflect the proceeds, up to ~45s total.
            import time
            for _ in range(15):
                time.sleep(3)
                try:
                    if api.get_order(so.id).status == 'filled':
                        break
                except Exception:
                    pass
            for _ in range(10):
                bp = float(api.get_account().buying_power)
                if bp >= notional + 5:
                    break
                time.sleep(3)
    qty = round(min(notional, max(bp - 5, 0)) / px, 4)
    if qty <= 0:
        log(f"  [LIVE-MIRROR] {sym}: no buying power even after SGOV trim -- skipped")
        return None
    try:
        o = api.submit_order(symbol=sym, qty=qty, side='buy', type='market', time_in_force='day')
    except Exception as e:
        if 'fractionable' in str(e).lower() and int(qty) > 0:
            o = api.submit_order(symbol=sym, qty=int(qty), side='buy', type='market', time_in_force='day')
            qty = int(qty)
        else:
            raise
    log(f"  [LIVE-MIRROR] ✓ BOUGHT {sym} qty={qty} @~${px:.2f} order={o.id[:8]}")
    return qty


def liquidate_all(api, reason, state):
    if DRY_RUN:
        import market_data as md
        sold_syms = []
        for sym, pos in list(state.get('sim_positions', {}).items()):
            q = md.get_stock_quote(sym)
            px = q['current'] if q and q.get('current') else pos['entry_price']
            plpc = (px - pos['entry_price']) / pos['entry_price'] * 100
            state['sim_cash'] = state.get('sim_cash', 0) + pos['qty'] * px
            log(f"  [DRY-RUN] ✓ SOLD {sym} qty={pos['qty']} @~${px:.2f} — {reason}")
            record_action(state, f"卖出 {sym} qty={pos['qty']} 盈亏{plpc:+.2f}% — {reason}")
            sold_syms.append(sym)
        state['sim_positions'] = {}
        save_state(state)
        live_sold = 0
        if MIRROR_TO_LIVE:
            for sym in sold_syms:
                try:
                    p = api.get_position(sym)
                    o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
                    log(f"  [LIVE-MIRROR] ✓ SOLD {sym} qty={p.qty} order={o.id[:8]} — {reason}")
                    record_action(state, f"[实盘同步] 卖出 {sym} qty={p.qty} — {reason}")
                    live_sold += 1
                except Exception as e:
                    log(f"  [LIVE-MIRROR] sell {sym} failed/none held: {e}")
        return live_sold
    positions = api.list_positions()
    for p in positions:
        try:
            o = api.submit_order(symbol=p.symbol, qty=p.qty, side='sell', type='market', time_in_force='day')
            plpc = float(p.unrealized_plpc) * 100
            log(f"  ✓ SOLD {p.symbol} qty={p.qty} order={o.id[:8]} — {reason}")
            record_action(state, f"卖出 {p.symbol} qty={p.qty} 盈亏{plpc:+.2f}% — {reason}")
        except Exception as e:
            log(f"  sell {p.symbol} failed: {e}")


def park_to_sgov(mirror_live=False):
    if DRY_RUN and not mirror_live:
        log("  [DRY-RUN] skipping SGOV park-back (simulation only, no real cash)")
        return
    api = get_alpaca()
    import time
    k, s, _ = _alpaca_creds()
    # 2026-07-31: after a mass close-out, sell fills take a while to settle into
    # buying power -- a single fixed 8s sleep raced that and the SGOV buy died
    # with "insufficient buying power" (leaving $34k in raw cash over a weekend).
    # Retry with backoff, recomputing available funds fresh each attempt, and
    # size against the LOWER of cash and buying_power.
    for attempt in range(1, 5):
        time.sleep(8 * attempt)
        acc = api.get_account()
        avail = min(float(acc.cash), float(acc.buying_power))
        if avail < 5:
            return
        r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/quotes/latest',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s})
        ask = r.json()['quote']['ap']
        if not ask or ask < 50:
            ask = 100.80  # sanity guard against a stale/empty after-hours book
        limit_px = round(ask + 0.05, 2)
        qty = round((avail - 2) / limit_px, 4)
        if qty <= 0:
            return
        try:
            ext = api.get_clock().is_open
            o = api.submit_order(symbol='SGOV', qty=qty, side='buy', type='limit',
                                  limit_price=limit_px, time_in_force='day',
                                  extended_hours=not ext)
            log(f"  parked ${avail:.2f} cash into {qty} SGOV @~${limit_px} order={o.id[:8]}")
            return
        except Exception as e:
            log(f"  SGOV park attempt {attempt} failed: {e}")
    raise RuntimeError("SGOV park failed after 4 attempts")


def _kronos_note(sym):
    # 2026-08-08: wired in as an AUXILIARY technical signal, explicitly NOT a
    # gate. On RKLB/AAOI/COHR it returned BEARISH with implausibly large
    # magnitude (-17% to -46% over 5 sessions) -- flagged then as "direction
    # trustworthy, magnitude not."
    #
    # 2026-08-10 backtest (9 historical cases across SPY/AAPL/MU/MSFT,
    # truncating real history to a past date and checking the actual 5-day
    # forward move): directional hit rate was only 4/9 (44%) -- worse than a
    # coin flip. SPY was called BEARISH at every single test point (0/3
    # correct) and AAPL 0/2, both with "100% consistency" reported regardless
    # of whether the call was right -- i.e. trend_consistency does not appear
    # to track actual confidence, and the model looks like it defaults to
    # BEARISH often rather than genuinely reading each situation. It DID
    # correctly call MU's continued decline and MSFT's mild uptrend (2/2
    # each), so it isn't pure noise, but this is not evidence strong enough
    # to treat as independent confirmation of anything -- downgraded here to
    # a low-weight curiosity, not a signal. Still not wired into the
    # picker's large-universe screen (same reason, now with data behind it).
    try:
        # 2026-08-10: was hardcoded to GPU 1, which this box's other jobs
        # (rl_pipeline.py/rl_lora_trainer.py) also use for RL training and
        # can already be heavily loaded -- silently contending for it risked
        # OOM/slowdown for either workload with no visibility either way.
        # Pick whichever GPU currently has the most free memory instead.
        if 'CUDA_VISIBLE_DEVICES' not in os.environ:
            try:
                out = subprocess.run(
                    ['nvidia-smi', '--query-gpu=index,memory.free',
                     '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=10).stdout
                free_by_gpu = [(int(mem), idx) for idx, mem in
                               (line.split(',') for line in out.strip().splitlines() if line.strip())]
                best_gpu = str(max(free_by_gpu)[1])
                os.environ['CUDA_VISIBLE_DEVICES'] = best_gpu
                log(f"  kronos: picked GPU {best_gpu} ({max(free_by_gpu)[0]}MB free)")
            except Exception as e:
                os.environ['CUDA_VISIBLE_DEVICES'] = '1'
                log(f"  kronos: GPU auto-select failed ({e}), falling back to GPU 1")
        sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')
        import kronos_analysis as ka
        import yfinance as yf
        hist = yf.Ticker(sym).history(period='2y')
        pred = ka.predict_next_candles(sym, hist)
        if not pred:
            return ""
        sig = pred['kronos_signal']
        cons = pred['trend_consistency']
        if sig == 'NEUTRAL' or cons < 0.6:
            return ""
        return (f" [Kronos K线模型信号(低权重参考,回测方向命中率仅44%且疑似有"
                f"看空偏置,不构成独立证据): {sig},一致性{cons:.0%}"
                f"——不要采信{pred['expected_return_pct']:+.1f}%这类具体幅度数字]")
    except Exception:
        return ""


def _upcoming_earnings_note(sym, lookahead_days=3):
    # 2026-08-08: added after noticing RKLB (~30% of the account) reports Q2
    # earnings 2026-08-10 -- a real near-term binary risk for a position this
    # large -- while the judge's held-position context had no way to know an
    # earnings date was coming at all. Cheap, per-symbol, degrades silently.
    try:
        import yfinance as yf
        ed = yf.Ticker(sym).get_earnings_dates(limit=2)
        if ed is None or ed.empty:
            return ""
        today = datetime.date.today()
        for dt, row in ed.iterrows():
            d = dt.date()
            delta = (d - today).days
            if 0 <= delta <= lookahead_days:
                est = row.get('EPS Estimate')
                has_est = est is not None and est == est  # NaN != NaN
                est_str = f",EPS预期${est:.2f}" if has_est else ""
                return f" [财报风险: {d.isoformat()}即将公布财报{est_str},注意仓位不要在报告前后过度集中]"
        return ""
    except Exception:
        return ""


def ai_judge_positions(api, state, day_pl_pct, mins_to_close, audit_mode=False):
    # Replaces the fixed FLOOR_PCT/CEILING_PCT/mandatory-close-out numbers with
    # the AI's own judgment call, per "全部去掉" + "让ai自己判断". Works for
    # both the virtual (DRY_RUN) and real (live, currently paused) ledger.
    # Returns (action, detail) where action in {'hold','sell_all','hold_overnight'}.
    if DRY_RUN:
        held = {sym: {'entry_price': pos['entry_price'], 'qty': pos['qty']}
                for sym, pos in state.get('sim_positions', {}).items()}
    else:
        held = {p.symbol: {'entry_price': float(p.avg_entry_price), 'qty': float(p.qty)}
                for p in api.list_positions()}
    if not held:
        return 'hold', None

    near_close = mins_to_close <= 20
    now = datetime.datetime.utcnow()
    if not near_close:
        last_judge = state.get('_last_judge_time')
        if last_judge:
            elapsed = (now - datetime.datetime.fromisoformat(last_judge)).total_seconds() / 60
            if elapsed < POSITION_JUDGE_COOLDOWN_MIN:
                return 'hold', None
    state['_last_judge_time'] = now.isoformat()
    save_state(state)

    import market_data as md
    lines = []
    today_str = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    aux_cache = state.setdefault('_aux_notes_cache', {})
    for sym, pos in held.items():
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else pos['entry_price']
        plpc = (px - pos['entry_price']) / pos['entry_price'] * 100
        # 2026-08-10: _upcoming_earnings_note/_kronos_note were being recomputed
        # on EVERY call, including every single minute during the near_close
        # window (mins_to_close<=20 bypasses the cooldown above) -- a fresh
        # yfinance fetch + full Kronos GPU model reload per held position per
        # minute, right when the mandatory close-out decision needs to run
        # promptly. Neither signal changes meaningfully within a day, so
        # cache both once per (symbol, date) instead of on every call.
        cached = aux_cache.get(sym)
        if cached and cached.get('date') == today_str:
            earnings_note, kronos_note = cached['earnings_note'], cached['kronos_note']
        else:
            earnings_note = _upcoming_earnings_note(sym)
            kronos_note = _kronos_note(sym)
            aux_cache[sym] = {'date': today_str, 'earnings_note': earnings_note, 'kronos_note': kronos_note}
        lines.append(f"{sym}: 入价${pos['entry_price']:.2f} 现价${px:.2f} 盈亏{plpc:+.2f}% "
                     f"理由:{state.get('reasons', {}).get(sym, '')}{earnings_note}{kronos_note}")

    # 2026-08-04: give the judge the MARKET benchmark alongside portfolio P&L.
    # On 08-03 it reasoned "-0.78% is normal fluctuation, hold" without knowing
    # SPY was +2% the same day -- lagging a strong tape by ~3pp is a materially
    # different situation than drifting with a flat one, and the judge couldn't
    # see that.
    spy_note = ""
    try:
        k2, s2, _ = _alpaca_creds()
        rs = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot',
                           headers={'APCA-API-KEY-ID': k2, 'APCA-API-SECRET-KEY': s2}, timeout=10)
        snap = rs.json()
        prev = snap['prevDailyBar']['c']
        lastpx = snap.get('latestTrade', {}).get('p')
        if prev and lastpx:
            spy_chg = (lastpx - prev) / prev * 100
            spy_note = (f"今日大盘(SPY): {spy_chg:+.2f}% -- 请把组合表现和大盘对比着判断:"
                        f"跑输强势大盘和跟随弱势大盘回调,是性质不同的两种情况。\n")
    except Exception:
        pass

    near_close_note = ("现在快收盘了,必须在 HOLD_OVERNIGHT 和 SELL_ALL 之间二选一,"
                        "不能只说HOLD。\n" if near_close else "")
    account_note = ("一个模拟盘(其每笔操作会同步镜像到真实资金账户,请按真实资金的审慎程度判断)"
                    if (DRY_RUN and MIRROR_TO_LIVE)
                    else ("一个模拟盘(无真实资金风险)" if DRY_RUN else "一个真实资金账户"))
    prompt = (
        f"你在管理{account_note}的日内交易组合,不受任何固定百分比"
        "止盈止损规则限制,完全靠你自己的判断决定接下来怎么做。\n\n"
        f"当日账户总盈亏: {day_pl_pct:+.2f}%\n{spy_note}距收盘约{mins_to_close:.0f}分钟\n"
        f"持仓明细:\n" + "\n".join(lines) + "\n\n"
        + (lessons_context_str(8) + "\n\n" if lessons_context_str(8) else "")
        + f"{near_close_note}"
        "判断每只持仓时必须考虑**利好剩余的持续性**:催化剂已经一次性兑现完的仓位"
        "(评级上调的弹升已走完、一次性收益已被消化),即使没亏也应该腾出来让位给"
        "还有兑现空间的标的;催化剂仍在持续兑现的仓位(长期合同、新市场放量)值得"
        "耐心甚至加仓。目标只有一个:让每一块钱都待在最能赚钱的地方。\n\n"
        + ("**今天开盘首次判断,必须先做逐仓审计再给结论**:对每只持仓明确回答——"
           "(a)浮盈相对其催化剂的合理空间,兑现度有多高?兑现度高的(比如短短几天"
           "已+20%以上)必须明确选择'落袋/减仓/继续持有'并给出理由,不允许含糊带过;"
           "(b)入场理由是否与教训清单冲突?冲突的优先处理;(c)有没有隔夜出现的新消息"
           "改变某只持仓的论文?(d)**检查单票集中度**:任何一只持仓占比是否超过35%?"
           "超过的话即使论文没破位,也应该SELL_SOME减仓一部分腾给其他标的——"
           "2026-08-06曾发生连续5次'合理的'ROTATE_TO把9只持仓吃成2只、单票集中"
           "到91.6%的真实事故,系统现在有硬性上限但不会自动拆分已经存在的超额集中,"
           "需要你主动发现并处理。审计结论写在理由里。\n\n" if audit_mode else "")
        + "请从下面选项中选一个,第一行只写选项名称(带符号列表时严格按示例格式),第二行写一句话理由:\n"
        "HOLD(继续持有,不操作)\n"
        "SELL_ALL(现在全部平仓)\n"
        "HOLD_OVERNIGHT(收盘后继续持有到下一个交易日)\n"
        "SELL_SOME: SYM1,SYM2(只卖掉指定的弱势持仓,其余继续持有——腾出的资金可能被重新配置)\n"
        "ROTATE_TO_SPY: SYM1,SYM2(卖掉指定持仓,腾出的资金立即换成SPY大盘——适合个股论文走坏但大盘强势时)\n"
        "ROTATE_TO 目标代码: SYM1,SYM2(卖掉指定弱势持仓,腾出的资金加仓到某只已持有的强势标的——"
        "当组合中出现催化剂持续兑现、明显领跑的赢家时,把弱势仓位的资金向它集中,不要让最强的仓位一直是最小的)\n\n"
        "**大盘(SPY)永远是主动的降风险选项,不是走投无路才想到的备选**:如果某只持仓已经"
        "占比过高、盘中波动剧烈到让你不安,或者找不到比大盘更好的个股去承接资金,主动选择"
        "ROTATE_TO_SPY 把部分仓位换成大盘、降低组合的整体波动,是完全正确、值得鼓励的操作,"
        "不代表判断失败。"
    )
    try:
        result = subprocess.run([CLAUDE_BIN, '-p', prompt, '--output-format', 'json'],
                                 capture_output=True, text=True, timeout=120,
                                 cwd='/data/qbao775/AlphaTrader')
        if result.returncode != 0:
            log(f"  ai_judge_positions failed: {result.stderr[:200]}")
            return ('sell_all' if near_close else 'hold'), '(AI判断调用失败,安全默认)'
        data = json.loads(result.stdout)
        answer = data.get('result', '').strip()
        cost = data.get('total_cost_usd', 0)
        log(f"  AI持仓判断(cost ${cost:.4f}):\n{answer}")
        resp_lines = [l.strip() for l in answer.splitlines() if l.strip()]
        action_line = resp_lines[0].upper() if resp_lines else ''
        detail = resp_lines[1] if len(resp_lines) > 1 else ''
        if 'SELL_ALL' in action_line:
            action = 'sell_all'
        elif 'HOLD_OVERNIGHT' in action_line:
            action = 'hold_overnight'
        elif 'SELL_SOME' in action_line or 'ROTATE_TO' in action_line:
            # Formats: "SELL_SOME: DBD,SUPN" / "ROTATE_TO_SPY: COLM,HII" /
            # "ROTATE_TO FTK: COLM,HII" (rotate weak names into a held winner).
            # Only symbols we actually hold are accepted; unknown target -> SPY.
            import re as _re
            listed = set(_re.findall(r'\b[A-Z]{1,5}\b', action_line.split(':', 1)[-1]))
            syms = sorted(listed & set(held.keys()))
            if 'ROTATE_TO' in action_line:
                m = _re.search(r'ROTATE_TO[ _]([A-Z]{1,5})\s*:', action_line)
                target = m.group(1) if m else 'SPY'
                if target != 'SPY' and target not in held:
                    log(f"  ROTATE_TO target {target} not held -- defaulting to SPY")
                    target = 'SPY'
                syms = [s3 for s3 in syms if s3 != target]
                if syms:
                    action = ('rotate_to', target, syms)
                else:
                    log("  ROTATE_TO named no valid held symbols -- treating as HOLD")
                    action = 'hold'
            elif syms:
                action = ('sell_some', None, syms)
            else:
                log("  SELL_SOME named no held symbols -- treating as HOLD")
                action = 'hold'
        else:
            action = 'hold'
    except Exception as e:
        log(f"  ai_judge_positions exception: {e}")
        action, detail = ('sell_all' if near_close else 'hold'), '(AI判断异常,安全默认)'

    if near_close and action == 'hold':
        log("  近收盘AI未给出明确隔夜/平仓决定 -- 安全默认为平仓")
        action, detail = 'sell_all', detail or '(近收盘判断不明确,安全默认平仓)'
    return action, detail


def sell_selected(api, state, syms, rotate_to, reason):
    # 2026-08-05: per-position exits ("继续优化") -- the judge used to be
    # all-or-nothing (HOLD / SELL_ALL / HOLD_OVERNIGHT), so it couldn't cut a
    # -3% laggard while a +13% winner kept running. Sells the named positions
    # on both ledgers; with rotate_to set, the freed capital immediately buys
    # that target instead of sitting idle. Same-day generalization (user:
    # "一只FTK的收益比全部的收益还高" -- FTK alone out-earned the whole book
    # while carrying one of the SMALLEST weights): rotate_to may be SPY *or
    # any currently-held name*, so the judge can consolidate capital into a
    # proven winner (动态调仓, the 2026-07-14 lesson), not just into the index.
    import market_data as md
    freed_w = 0.0
    equity_paper, _ = get_account_view(api, state)
    for sym in syms:
        pos = state.get('sim_positions', {}).get(sym)
        if not pos:
            continue
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else pos['entry_price']
        plpc = (px - pos['entry_price']) / pos['entry_price'] * 100
        state['sim_cash'] = state.get('sim_cash', 0) + pos['qty'] * px
        freed_w += (pos['qty'] * px) / equity_paper
        del state['sim_positions'][sym]
        log(f"  [DRY-RUN] ✓ SOLD {sym} qty={pos['qty']} @~${px:.2f} — {reason}")
        record_action(state, f"卖出 {sym} 盈亏{plpc:+.2f}% — {reason}")
        if sym != 'SPY':
            record_watch_back(sym, px, reason)
        state.setdefault('weights', {}).pop(sym, None)
        save_state(state)
        if MIRROR_TO_LIVE:
            try:
                p = api.get_position(sym)
                o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
                log(f"  [LIVE-MIRROR] ✓ SOLD {sym} qty={p.qty} order={o.id[:8]}")
            except Exception as e:
                log(f"  [LIVE-MIRROR] sell {sym} failed/none held: {e}")
    if not rotate_to or freed_w <= 0:
        # freed capital may be redeployed -- let the idle-capital rescan look again
        state['second_scan_done'] = False
        save_state(state)
        return

    def _buy_into(sym, w, tag):
        # Shared buy path for both the intended rotation target and any
        # overflow that gets redirected into SPY (below) -- keeps ledger
        # update + live mirror consistent for both cases.
        qq = md.get_stock_quote(sym)
        pxx = qq['current'] if qq and qq.get('current') else None
        if not pxx:
            log(f"  rotate: no {sym} quote, leaving that portion in cash")
            return
        notional_ = equity_paper * w
        add_qty_ = round(notional_ / pxx, 4)
        sp_ = state.setdefault('sim_positions', {})
        if sym in sp_:
            old_ = sp_[sym]
            new_qty_ = old_['qty'] + add_qty_
            sp_[sym] = {'qty': new_qty_,
                        'entry_price': round((old_['qty']*old_['entry_price'] + add_qty_*pxx) / new_qty_, 4)}
        else:
            sp_[sym] = {'qty': add_qty_, 'entry_price': pxx}
        state['sim_cash'] = state.get('sim_cash', 0) - notional_
        state.setdefault('weights', {})[sym] = state.get('weights', {}).get(sym, 0) + w
        state.setdefault('reasons', {}).setdefault(sym, '弱势仓位轮换目标(ROTATE_TO)' if sym != 'SPY' else '风险敞口超限,超额部分自动降级为大盘')
        save_state(state)
        log(f"  [DRY-RUN] ✓ ROTATED into {sym} qty={add_qty_} @~${pxx:.2f} ({tag})")
        record_action(state, f"轮换买入 {sym} {add_qty_}股 @~${pxx:.2f} ({tag})")
        if MIRROR_TO_LIVE:
            try:
                live_qty_ = mirror_live_buy(api, sym, w, pxx)
                if live_qty_:
                    record_action(state, f"[实盘同步] 轮换买入 {sym} {live_qty_}股")
            except Exception as e:
                log(f"  [LIVE-MIRROR] rotate buy {sym} FAILED -- reconciler will retry: {e}")

    # 2026-08-06/07: REAL INCIDENT -- ROTATE_TO fired 5 times in ~2 hours
    # (each judged sound in isolation) and consolidated a 9-name book down to
    # just FTK+RKLB at 91.6% of the account; the next day the account swung
    # -$1,700 intraday purely off FTK's own volatility (user: "过山车太惊
    # 险了" + "应该要保留选择大盘的可能"). Two caps bound the cascade AND
    # its concentration -- and critically, whenever either cap diverts
    # capital, that capital now lands in SPY instead of idle cash, so
    # de-risking into the index is the automatic, structural fallback, not
    # just an option the judge might remember to pick.
    rotate_count = state.get('_rotate_count_today', 0)
    if rotate_count >= MAX_ROTATIONS_PER_DAY:
        log(f"  rotate: already rotated {rotate_count}x today (cap {MAX_ROTATIONS_PER_DAY}) -- "
            f"routing this rotation into SPY instead of concentrating further into {rotate_to}")
        _buy_into('SPY', freed_w, f"当日调仓次数已达上限,超额部分自动转为大盘(原目标 {rotate_to})")
        return
    state['_rotate_count_today'] = rotate_count + 1
    save_state(state)

    current_tgt_w = state.get('weights', {}).get(rotate_to, 0)
    capped_w, spy_w = freed_w, 0.0
    if rotate_to != 'SPY' and current_tgt_w + freed_w > MAX_SINGLE_NAME_CONCENTRATION_PCT:
        capped_w = max(0.0, MAX_SINGLE_NAME_CONCENTRATION_PCT - current_tgt_w)
        spy_w = freed_w - capped_w
        log(f"  rotate: capping {rotate_to} at {MAX_SINGLE_NAME_CONCENTRATION_PCT*100:.0f}% "
            f"concentration -- using {capped_w*100:.1f}% of the freed {freed_w*100:.1f}%, "
            f"routing the remaining {spy_w*100:.1f}% into SPY instead of idle cash")
    if capped_w > 0:
        _buy_into(rotate_to, capped_w, "弱势仓位换强势目标")
    if spy_w > 0:
        _buy_into('SPY', spy_w, f"{rotate_to}集中度已达上限,超额部分自动转为大盘")


RECONCILE_RETRY_SECONDS = 240   # per-symbol cooldown between live re-buy attempts
LONG_TERM_NAMES_NEVER_TOUCH = {'SGOV', 'SKHY', 'MU', 'META'}


def reconcile_live_with_paper(api, state, mins_to_close):
    # 2026-08-04: self-healing sync -- a failed mirror buy used to stay failed
    # forever (5 in one session on 08-03: live ended up missing BABA/ABT/DBD/
    # FTK/SUPN while paper was fully deployed). Every managed tick now compares
    # live holdings against the paper ledger and retries any missing buy at the
    # current price (per-symbol cooldown so a genuinely-broken symbol doesn't
    # get hammered). Never touches SGOV or the long-term thesis names; never
    # opens new positions in the final minutes before close.
    if not (DRY_RUN and MIRROR_TO_LIVE) or mins_to_close <= 25:
        return
    sim = state.get('sim_positions', {})
    try:
        live_syms = {p.symbol for p in api.list_positions()}
    except Exception:
        return
    now = datetime.datetime.utcnow()
    attempts = state.setdefault('_reconcile_attempts', {})
    for sym, pos in sim.items():
        if sym in live_syms or sym in LONG_TERM_NAMES_NEVER_TOUCH:
            continue
        last = attempts.get(sym)
        if last and (now - datetime.datetime.fromisoformat(last)).total_seconds() < RECONCILE_RETRY_SECONDS:
            continue
        attempts[sym] = now.isoformat()
        save_state(state)
        w = state.get('weights', {}).get(sym)
        if not w:
            continue
        import market_data as md
        q = md.get_stock_quote(sym)
        px = q['current'] if q and q.get('current') else None
        if not px:
            continue
        log(f"  [RECONCILE] live is missing {sym} (paper holds it) -- retrying the mirror buy")
        try:
            live_qty = mirror_live_buy(api, sym, w, px)
            if live_qty:
                record_action(state, f"[实盘同步补齐] 买入 {sym} {live_qty}股 @~${px:.2f} (此前同步失败,已自动补上)")
        except Exception as e:
            log(f"  [RECONCILE] retry buy {sym} failed again: {e}")

    # Opposite direction (2026-08-05): paper sold something but the live mirror
    # sell failed -- live would otherwise keep an unmanaged extra forever. Sell
    # any live position the paper ledger no longer holds (long-term names and
    # SGOV excluded).
    for sym in sorted(live_syms - set(sim.keys()) - LONG_TERM_NAMES_NEVER_TOUCH):
        key = f"extra:{sym}"
        last = attempts.get(key)
        if last and (now - datetime.datetime.fromisoformat(last)).total_seconds() < RECONCILE_RETRY_SECONDS:
            continue
        attempts[key] = now.isoformat()
        save_state(state)
        log(f"  [RECONCILE] live holds {sym} that paper does not -- selling the orphan")
        try:
            p = api.get_position(sym)
            o = api.submit_order(symbol=sym, qty=p.qty, side='sell', type='market', time_in_force='day')
            record_action(state, f"[实盘同步纠偏] 卖出多余持仓 {sym} {p.qty}股 (模拟盘已不持有)")
        except Exception as e:
            log(f"  [RECONCILE] orphan sell {sym} failed: {e}")


def manage(api, state):
    equity, bp = get_account_view(api, state)
    day_start_equity = state['day_start_equity']
    day_pl_pct = (equity - day_start_equity) / day_start_equity * 100

    clock = api.get_clock()
    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60

    # "时刻要和大盘收益比较" -- every managed tick logs the portfolio vs SPY
    spy_chg, _ = spy_day_change()
    if spy_chg is not None:
        rel = day_pl_pct - spy_chg
        log(f"  组合当日 {day_pl_pct:+.2f}% vs SPY {spy_chg:+.2f}% ({'跑赢' if rel >= 0 else '跑输'} {abs(rel):.2f}pp)")

    reconcile_live_with_paper(api, state, mins_to_close)

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

    # Exits are entirely AI-judged now (no fixed floor/ceiling/close-out numbers --
    # see ai_judge_positions()), for both the paper and (currently paused) live path.
    def _end_holdover_day():
        # 2026-08-04: hold-overnight days used to end silently -- finalize_day
        # never runs, so NO summary email and NO history entry were produced
        # (user got nothing on 08-03's -0.64% overnight-hold day). Close the
        # day's books explicitly while keeping the positions.
        state['done'] = True
        save_state(state)
        if not state.get('_eod_reported'):
            state['_eod_reported'] = True
            save_state(state)
            append_history({'date': state['date'], 'weights': state.get('weights', {}),
                             'reasons': state.get('reasons', {}), 'final_pl_pct': round(day_pl_pct, 2),
                             'spy_pct': spy_day_change()[0],
                             'reason': 'AI判断隔夜持有,仓位带入下一交易日'})
            send_daily_summary(state, day_pl_pct, "AI判断隔夜持有,仓位带入下一交易日(未平仓,盈亏为浮动值)")

    if state.get('hold_overnight'):
        # Already decided this session -- don't re-ask every tick in the
        # last 20 minutes (wasteful + risks a flip-flopping answer).
        # Just wait for the close to actually arrive, then close the books.
        if mins_to_close <= 2:
            _end_holdover_day()
        return
    # 2026-08-06: mandatory morning audit -- the first judge call of each day
    # must audit every carried position's catalyst-realization degree, lesson
    # conflicts, and overnight news BEFORE concluding (user: "这些你应该自己
    # 检查，不要我来提醒你" -- e.g. FTK sat at +31% with its catalyst largely
    # priced in, and nothing forced the question until the user asked).
    audit_mode = not state.get('_morning_audit_done', False)
    action, detail = ai_judge_positions(api, state, day_pl_pct, mins_to_close, audit_mode=audit_mode)
    if audit_mode and state.get('_last_judge_time'):
        state['_morning_audit_done'] = True
        save_state(state)
        record_action(state, f"开盘逐仓审计完成: {detail[:200] if detail else '(见AI判断日志)'}")
    if action == 'sell_all':
        reason = f"AI判断: {detail or '主动平仓'} (当日盈亏 {day_pl_pct:+.2f}%)"
        finalize_day(api, state, day_pl_pct, reason)
        return
    if action == 'hold_overnight':
        state['hold_overnight'] = True
        save_state(state)
        log(f"  AI决定隔夜持有: {detail}")
        record_action(state, f"AI判断隔夜持有: {detail} (当日盈亏 {day_pl_pct:+.2f}%)")
        if mins_to_close <= 2:
            _end_holdover_day()
        return
    if isinstance(action, tuple):
        kind, target, syms = action
        reason = f"AI判断({kind}{' -> ' + target if target else ''}): {detail or '部分调仓'}"
        log(f"  AI decided {kind} -> {target}: {syms} -- {detail}")
        sell_selected(api, state, syms, rotate_to=target, reason=reason)
        if not state.get('sim_positions'):
            # partial sell emptied the whole book -- close the day's books properly
            finalize_day(api, state, day_pl_pct, reason, do_liquidate=False)
        return
    # action == 'hold': fall through, keep ticking

    # 2026-07-16: second-chance re-scan -- user asked to raise the probability
    # of clearing the floor, and this is a legitimate way to do it (more
    # independent looks at the market, not more risk per look). If the floor
    # hasn't been touched after SECOND_SCAN_AFTER_MIN and there's still real
    # uncommitted buying power, take one more look for fresh catalysts.
    elapsed_min = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(state['day_start_time'])).total_seconds() / 60
    # 2026-08-11: REAL INCIDENT -- this used to be sum(state['weights'].values()),
    # a target-weight ledger that goes stale the moment capital moves through
    # any path OTHER than a fresh pick entry (a sell_some with no rotate_to
    # pops the symbol's weight with nowhere for that fraction to go; capital
    # that flows into an EXISTING position, like SPY absorbing a sale's
    # proceeds, never gets added back to that symbol's recorded weight
    # either). Concretely: after this morning's RKLB sale (popped ~29% from
    # weights) fed straight into topping up the already-held SPY position,
    # this computed a stale ~74.6% ("room=25.4%") when the account was
    # actually already ~100%+ deployed -- triggering a $0.52 second-chance
    # scan whose 7 real picks then got scaled down to fit that phantom room
    # instead of their intended weights. Since SPY is now reclaimable
    # on-demand for a confirmed pick (see enter()'s SPY-trim logic), what
    # actually matters is how much equity is committed to NON-reclaimable
    # individual positions -- compute that directly from real position
    # values instead of the drifting weights ledger.
    _positions_for_room = (state.get('sim_positions', {}) if DRY_RUN else
                            {p.symbol: {'qty': float(p.qty)} for p in api.list_positions()})
    _non_reclaimable_value = 0.0
    import market_data as _md
    for _sym, _pos in _positions_for_room.items():
        if _sym == 'SPY':
            continue
        _q = _md.get_stock_quote(_sym)
        _px = _q['current'] if _q and _q.get('current') else _pos.get('entry_price')
        if _px:
            _non_reclaimable_value += _pos['qty'] * _px
    current_total_w = (_non_reclaimable_value / equity) if equity else 0.0
    underperforming = day_pl_pct < FLOOR_PCT
    idle_capital = current_total_w < MIN_DEPLOYED_PCT_BEFORE_RESCAN
    if (not os.path.exists(NEW_ENTRIES_PAUSED_FILE)
            and (underperforming or idle_capital) and not state.get('second_scan_done')
            and elapsed_min >= SECOND_SCAN_AFTER_MIN and mins_to_close > 30):
        state['second_scan_done'] = True
        save_state(state)
        why = "underperforming" if underperforming else "idle capital, current picks already doing fine"
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
        room = max(0.0, MAX_TOTAL_DEPLOY_PCT - current_total_w)
        if room > 0.01:  # only bother if there's meaningful cap room left
            log(f"  {why} after {elapsed_min:.0f}min -- running a second-chance scan (room={room*100:.1f}%)")
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


def acquire_singleton_lock():
    # 2026-07-31: REAL-MONEY INCIDENT -- the rate-limit pacing added earlier today
    # made the 13:30 picker run take >4 minutes, so the 13:31-13:34 cron ticks all
    # started their own overlapping processes: each re-ran the picker on stale
    # state, LMT got bought TWICE on the live account (double the intended 7%),
    # a stale re-pick bought CELC on paper that live never held, and concurrent
    # save_state() writers clobbered each other's position records. Same failure
    # class as the 2026-07-15 AEHR manual/cron race, now made structurally
    # impossible: only one instance may run at a time -- later ticks exit
    # immediately instead of queueing (the next minute's tick picks up anyway).
    # The cron line ALSO wraps with flock -n; this in-code guard survives someone
    # copying the cron line without it.
    import fcntl
    lockf = open('/tmp/dod_daytrade_singleton.lock', 'w')
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return lockf  # keep the handle alive for the process lifetime


def main():
    api = get_alpaca()
    clock = api.get_clock()
    today = clock.timestamp.strftime('%Y-%m-%d')
    state = load_state()

    if state.get('date') != today:
        if state.get('hold_overnight'):
            # 2026-07-22: AI chose to carry positions into the next trading
            # day instead of a forced close-out (see ai_judge_positions) --
            # keep symbols/weights/reasons/sim_positions/sim_cash, only reset
            # the per-day bookkeeping (P&L baseline resets so "today's P&L"
            # reflects just today, not a stale multi-day mix).
            log(f"=== new trading day {today} -- carrying over overnight positions per AI judgment ===")
            state['date'] = today
            state['done'] = False
            state['hold_overnight'] = False
            state['action_log'] = []
            for k in ('day_start_equity', 'day_start_time', 'floor_armed',
                      'second_scan_done', '_last_judge_time', 'skipped_regime',
                      '_morning_audit_done', '_reconcile_attempts', '_eod_reported',
                      '_rotate_count_today'):
                state.pop(k, None)
        else:
            state = {'date': today, 'symbols': {}, 'weights': {}, 'reasons': {},
                      'action_log': [], 'done': False}
            log(f"=== new trading day {today} -- state reset ===")
        save_state(state)

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

    if not state.get('weights') and not state.get('skipped_regime') and os.path.exists(NEW_ENTRIES_PAUSED_FILE):
        log("  new entries paused (code under active development) -- staying in cash/SGOV")
        record_action(state, "新开仓已暂停(代码还在更新),今天继续持有美债")
        finalize_day(api, state, 0.0, "代码更新期间暂停新开仓", do_liquidate=False)
        return

    if not state.get('weights') and not state.get('skipped_regime'):
        ok, chg_pct = market_regime_ok(api)
        # 2026-07-21/22: user wants full AI judgment -- "不管什么情况都是让ai自己决定
        # 买卖" then "全部去掉"/"让ai自己判断" for the live path too. The SPY-down
        # gate no longer hard-skips the day for either path; it's passed as CONTEXT
        # into the picker so the AI weighs it itself (it can still return NONE via
        # "宁缺毋滥" if it judges conditions too weak -- the point is it's the AI's
        # call every time, not a fixed mechanical rule).
        # 2026-08-04: on 08-03 SPY rose ~+2% while the all-single-name catalyst
        # basket LOST -0.64% -- the picker structurally had no "just hold the
        # index" option, so it was forced into stock-picking even on a broad
        # macro-rally day when index exposure was the obvious baseline (user:
        # "大盘都在涨，我们还在亏这个错误不应该啊"). The index is now always a
        # first-class candidate: the picker sees SPY's live change either way
        # and is explicitly told SPY/QQQ are valid picks that individual names
        # must BEAT, not just match.
        if chg_pct is not None:
            direction = "走强" if chg_pct >= 0 else "走弱"
            # 2026-08-07: user, after the -$1,700 concentration scare -- "把标普500
            # 作为一个baseline，除非你看到有收益率更好的选择，否则就还是买标普500"
            # (treat SPY as the baseline; unless you see a clearly better return,
            # just hold SPY). This inverts the prior framing from "cash is the
            # default, individual stocks are the goal" to "SPY is the default,
            # individual stocks must clear a real bar to replace part of it."
            regime_note = (
                f"大盘实时状态: SPY 今日{direction} {chg_pct:+.2f}%。\n"
                "**核心原则:把持有SPY(大盘)当作今天的默认基准仓位**,不是"
                "退而求其次的备选。只有当某只个股的预期回报明显优于直接持有SPY"
                "时,才把对应比例的资金分配给它;个股权重合计之外的部分,默认"
                "留在SPY里,不是现金。如果今天完全找不到明显优于SPY的机会,"
                "只输出 SPY: 100% [持续性:数周+] 大盘基准仓位 这一行,这是"
                "完全正常、值得肯定的结果,不是'没做好功课'。反之如果大盘本身"
                "走弱,请自行判断今天是否仍要建仓,或空仓持有美债更稳妥。\n\n"
            )
        else:
            regime_note = ""
        if not ok:
            log(f"  SPY pre-market/today {chg_pct:+.2f}% -- weak, letting the AI itself decide")
        exclude = already_held_elsewhere(api)
        picks, cost = pick_todays_stocks(api, exclude=exclude, extra_note=regime_note)
        if not picks:
            if ok:
                # 2026-08-07: SPY-as-baseline default -- a NONE verdict on a
                # non-weak day means "nothing beats the baseline", so the
                # baseline itself (SPY) is what we hold, not idle cash.
                log("  no individual picks beat the SPY baseline -- defaulting to SPY itself")
                picks = [('SPY', MAX_TOTAL_DEPLOY_PCT, '无个股明显优于大盘,默认持有SPY基准仓位')]
                cost = cost or 0.0
            else:
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
    _lock = acquire_singleton_lock()
    if _lock is None:
        # Another instance is mid-run (e.g. a slow picker) -- skip this tick
        # silently; the next minute's tick will land once it finishes.
        sys.exit(0)
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION this tick:")
        log(traceback.format_exc())
