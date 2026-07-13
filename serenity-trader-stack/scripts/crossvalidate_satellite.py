#!/usr/bin/env python3
"""
crossvalidate_satellite.py — Plan C hybrid automation (ratified 2026-07-03).

Every 4 hours (via cron), for each CURRENT satellite holding (pulled live
from Alpaca, not hardcoded):
  1. FREE local pass: two independent quick reads via the already-running
     local Ollama model (gemma4:31b, :11435 — same model news_watch.py uses,
     zero marginal GPU cost):
       a) "4-master lite" — condensed Buffett/Munger/段永平/李录 verdict
          (approximates ai-berkshire's /investment-team, since that skill
          needs a live Claude Code session with Team/Task orchestration and
          cannot be cron-triggered — see PLAN_D.md for why)
       b) Serenity chokepoint re-check — "is the original thesis still
          intact", not a fresh screen
  2. Cross-validate the two verdicts. If they DISAGREE, or either flags a
     thesis break, or price has moved >15% from cost basis, or it's been
     >7 days since the last paid deep-dive on this ticker — ESCALATE.
  3. PAID escalation only: call `claude -p` (real $, ~$0.05-0.15/call per
     the 2026-07-03 test) for a genuine deep-dive synthesizing both lenses.
  4. Log everything to reports/<TICKER>/updates.md (git-committed). Email
     ONLY on escalation or a flagged concern — routine "still fine" local
     checks stay silent, matching news_watch.py's "alert on fresh material
     items only" pattern.

THIS SCRIPT NEVER PLACES ORDERS. Read-only market data + position queries
only. No alpaca_trade_api submit_order call exists anywhere in this file.
Decision-support only, per the standing mandate (~/serenity-trader-stack/PLAN_D.md).
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

REPORTS_DIR = '/home/qbao775/serenity-trader-stack/reports'
STATE_FILE = '/home/qbao775/serenity-trader-stack/.crossvalidate_state.json'
OLLAMA_HOST = 'http://localhost:11435'
OLLAMA_MODEL = 'gemma4:31b'
PRICE_MOVE_TRIGGER_PCT = 15.0   # escalate if unrealized P&L moves beyond this
STALE_DEEPDIVE_DAYS = 7         # force a paid check-in even if nothing else fires
# 2026-07-07: during real GPU contention on this shared server, "both local
# checks timed out" fired on 3 consecutive 4h cycles (08:00/12:00/12:00),
# each paying for a claude -p call that just said "infra issue, not a real
# signal" -- $1.94 total for zero new information. Don't pay again for the
# SAME reason within this window; genuine signals (disagreement/BEARISH/
# price move/staleness) still escalate immediately regardless.
#
# Bug fix (2026-07-12): this was set to 3h against a cron that runs every 4h
# (0 */4 * * *) -- since every normal run is already >3h after the previous
# one, the cooldown could never actually suppress anything in steady state;
# it only helped for a manual rerun close to the last one. Confirmed live:
# crossvalidate.log shows this same infra-only escalation firing repeatedly
# across ordinary 4h-spaced cycles (2026-07-10 08:00 and 12:00 both paid for
# "Ollama down" when the daemon was never actually down, just contended).
# Needs to be >= the cron interval to do anything; 5h gives one real cycle of
# headroom without silencing a genuinely new escalation for too long.
INFRA_FAILURE_COOLDOWN_HOURS = 5
USER_EMAIL = 'bqmbill714@gmail.com'

# 2026-07-07: user asked to also proactively screen NEW buy candidates every
# 4h, not just recheck existing satellite holdings — starting with Korea/
# global chip exposure (EWY is the only one of these actually tradeable via
# Alpaca right now; Moomoo has no KR market enabled, IBKR isn't connected —
# see .broker_capabilities.json). Extend this list as new candidates surface.
CANDIDATE_WATCHLIST = ['EWY', 'NVDA', 'COHR', 'LITE']
TRIAL_BUCKET_PCT = 0.03           # 试探仓 per CLAUDE.md rule 5
CHASE_GUARD_INTRADAY_PCT = 5.0    # don't buy if already up this much today


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def safety_check():
    """Informational only — this script never trades, but log a warning if
    the rogue legacy engine is somehow running again (see PLAN_D.md incident)."""
    try:
        out = subprocess.run(['pgrep', '-f', 'start\\.sh|main:app'],
                             capture_output=True, text=True).stdout.strip()
        if out:
            log(f"⚠️ WARNING: legacy engine processes detected (PIDs: {out}) — "
                f"this script does not trade, but flag for manual follow-up")
        kill_switch = '/data/qbao775/AlphaTrader/.DISABLE_AUTOSTART'
        if not os.path.exists(kill_switch):
            log(f"⚠️ WARNING: kill-switch file missing ({kill_switch}) — "
                f"recreate it, see PLAN_D.md CRITICAL INCIDENT section")
    except Exception as e:
        log(f"safety_check error (non-fatal): {e}")


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


def get_satellite_positions():
    """Pull LIVE positions from Alpaca. Excludes core Plan D holdings
    (SPY/QQQ/BRK.B) — those are long-term passive, not subject to this
    satellite thesis-tracking loop."""
    from database import SessionLocal, get_setting
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    api = tradeapi.REST(k, s, u)
    CORE = {'SPY', 'QQQ', 'BRK.B'}
    positions = []
    for p in api.list_positions():
        if p.symbol in CORE:
            continue
        if float(p.market_value) < 1:
            continue
        positions.append({
            'symbol': p.symbol,
            'qty': float(p.qty),
            'cost_basis': float(p.cost_basis),
            'market_value': float(p.market_value),
            'unrealized_plpc': float(p.unrealized_plpc) * 100,
        })
    return positions


def ollama_call(prompt, timeout=300):
    # 2026-07-07: bumped 120s -> 240s. This is a shared 8-GPU lab server;
    # under real contention (other jobs on the same GPUs) a 31B-model
    # generation call can legitimately take >120s, which was triggering
    # unnecessary paid claude -p escalations (both local checks "failing"
    # when the model just hadn't finished yet, not because it's down).
    #
    # Bug fix (2026-07-12): 240s still wasn't enough -- crossvalidate.log
    # shows this timing out repeatedly even at 240s (e.g. 2026-07-10 08:04
    # and 12:04 UTC), confirming contention on this box can run longer than
    # that. Bumped again to 300s and added one retry -- a second attempt
    # after a cold-start has usually already paid the model-load cost, so
    # it's cheap insurance against declaring "infra failure" (which triggers
    # a paid claude -p escalation) over what's actually just slowness.
    import requests
    for attempt in (1, 2):
        try:
            r = requests.post(f"{OLLAMA_HOST}/api/generate",
                              json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                              timeout=timeout)
            if r.status_code == 200:
                return r.json().get('response', '').strip()
        except Exception as e:
            log(f"ollama_call failed (attempt {attempt}/2): {e}")
    return ""


def quick_4master_take(symbol, thesis_summary, price_context):
    """Condensed Buffett/Munger/段永平/李录 verdict. This approximates
    ai-berkshire's /investment-team, which cannot itself be cron-triggered
    (it requires live Claude Code Team/Task orchestration)."""
    prompt = (
        f"You are running a condensed 4-master investment check on {symbol}.\n"
        f"Existing thesis: {thesis_summary}\n\n"
        f"Live position data (authoritative — do NOT rely on your own recalled/trained price for {symbol}, "
        f"use only this): {price_context}\n\n"
        "Give ONE line per master, terse:\n"
        "BUFFETT: <moat/economics verdict — HOLD/SELL/WATCH + one clause>\n"
        "MUNGER: <inversion check — what would have to be true for this to be a mistake, one clause>\n"
        "DUAN(段永平): <is this a business you'd want to own for 10 years, one clause>\n"
        "LI_LU(李录): <long-term compounding + risk-of-permanent-loss verdict, one clause>\n"
        "OVERALL: <BULLISH/NEUTRAL/BEARISH>\n"
    )
    return ollama_call(prompt)


def quick_serenity_recheck(symbol, thesis_summary, price_context):
    """Re-verify the ORIGINAL chokepoint thesis is still intact — not a
    fresh screen. Uses the same skeptical-by-default framing as news_watch.py."""
    prompt = (
        f"You are re-checking a Serenity chokepoint thesis for {symbol} — "
        f"NOT screening it fresh, verifying if it's STILL TRUE.\n"
        f"Original thesis: {thesis_summary}\n\n"
        f"Live position data (authoritative — do NOT rely on your own recalled/trained price for {symbol}, "
        f"use only this; do NOT flag a price/valuation discrepancy against any other number): {price_context}\n\n"
        "Answer in this exact format:\n"
        "CHOKEPOINT_INTACT: <YES/WEAKENING/BROKEN>\n"
        "REASON: <one clause — cite any new evidence if you have general knowledge of it>\n"
        "OVERALL: <BULLISH/NEUTRAL/BEARISH>\n"
    )
    return ollama_call(prompt)


def get_live_price(symbol):
    """Real-time last trade (INCLUDING pre/post-market prints) straight from
    Alpaca's own v2 data API — yfinance's ticker.history() only returns
    completed daily bars, so it misses pre-market moves entirely. The old
    alpaca_trade_api SDK helpers (get_last_trade/get_last_quote) hit a
    deprecated v1 path and 404 — call the v2 endpoint directly instead."""
    from database import SessionLocal, get_setting
    import requests
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    try:
        r = requests.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=10)
        if r.status_code == 200:
            trade = r.json().get('trade', {})
            return trade.get('p'), trade.get('t')
    except Exception as e:
        log(f"  get_live_price({symbol}) failed: {e}")
    return None, None


def get_candidate_context(symbol):
    """Fresh quote for a NOT-yet-held candidate (no position data exists).
    Price comes from Alpaca's real-time feed (covers pre/post-market);
    52w range/fundamentals still come from yfinance since those don't need
    to be real-time-precise."""
    import market_data as md
    try:
        live_price, live_ts = get_live_price(symbol)
        q = md.get_stock_quote(symbol) or {}
        price = live_price if live_price else q.get('current')
        prev_close = q.get('current') - (q.get('change') or 0) if q.get('current') and q.get('change') is not None else None
        change_pct = ((price / prev_close - 1) * 100) if price and prev_close else q.get('change_pct')
        if not price:
            return None
        return {
            'price': price,
            'price_as_of': live_ts,
            'change_pct': change_pct,
            'fifty_two_week_high': q.get('fifty_two_week_high'),
            'fifty_two_week_low': q.get('fifty_two_week_low'),
        }
    except Exception as e:
        log(f"  get_candidate_context({symbol}) failed: {e}")
        return None


def quick_4master_new_screen(symbol, context_str):
    """Fresh 4-master screen on a NEW candidate (not a recheck — no existing thesis)."""
    prompt = (
        f"You are running a condensed 4-master FIRST-LOOK screen on {symbol} as a "
        f"POTENTIAL NEW satellite buy (not an existing position).\n"
        f"Live market data (authoritative — do not use your own recalled price): {context_str}\n\n"
        "Give ONE line per master, terse:\n"
        "BUFFETT: <moat/economics verdict — BUY/WATCH/PASS + one clause>\n"
        "MUNGER: <inversion check — what would have to be true for this to be a mistake, one clause>\n"
        "DUAN(段永平): <is this a business you'd want to own for 10 years, one clause>\n"
        "LI_LU(李录): <long-term compounding + risk-of-permanent-loss verdict, one clause>\n"
        "OVERALL: <BULLISH/NEUTRAL/BEARISH>\n"
    )
    return ollama_call(prompt)


def quick_serenity_new_screen(symbol, context_str):
    """Fresh Serenity chokepoint screen on a NEW candidate — is there a real
    supply-chain bottleneck here worth a position, not just a general holding."""
    prompt = (
        f"You are running a FIRST-LOOK Serenity chokepoint screen on {symbol} as a "
        f"POTENTIAL NEW satellite buy.\n"
        f"Live market data (authoritative — do not use your own recalled price): {context_str}\n\n"
        "Answer in this exact format:\n"
        "CHOKEPOINT_INTACT: <YES/WEAKENING/BROKEN> (YES = there is a real, defensible bottleneck here)\n"
        "REASON: <one clause>\n"
        "OVERALL: <BULLISH/NEUTRAL/BEARISH>\n"
    )
    return ollama_call(prompt)


def escalate_new_candidate(symbol, context_str, master_take, serenity_take):
    """PAID call — only when both local lenses independently agree BULLISH.
    Asks specifically for a buy/no-buy + sizing recommendation."""
    prompt = (
        f"本地两套框架(4大师 + Serenity卡点)独立筛选后,都对新标的 {symbol} 给出 BULLISH 首轮判断,"
        f"触发人工复核。\n\n"
        f"实时行情: {context_str}\n\n"
        f"本地4大师速览:\n{master_take}\n\n"
        f"本地Serenity速览:\n{serenity_take}\n\n"
        "请你做一次简明判断(3-5句话): 这是不是一个值得建仓的真实机会,还是本地模型的误判/凑巧一致?"
        "给出 BUY/PASS 的结论,如果 BUY,建议仓位是试探(≤3%)/标准(~5%)/确信(8-10%)中的哪一档。"
    )
    try:
        claude_bin = '/home/qbao775/.local/bin/claude'
        result = subprocess.run(
            [claude_bin, '-p', prompt, '--output-format', 'json'],
            capture_output=True, text=True, timeout=180,
            cwd='/data/qbao775/AlphaTrader'
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            cost = data.get('total_cost_usd', 0)
            answer = data.get('result', '')
            log(f"  claude -p cost: ${cost:.4f}")
            return answer, cost
        else:
            log(f"  claude -p failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"  claude -p exception: {e}")
    return "", 0


def execute_trial_buy(symbol, price):
    """Places a GTC limit buy sized to the trial bucket (~3% of equity),
    capped by genuinely-available cash (no-margin account). Returns
    (qty, limit_price, order_id) or None if it couldn't place anything."""
    from database import SessionLocal, get_setting
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    api = tradeapi.REST(k, s, u)

    acc = api.get_account()
    equity = float(acc.equity)
    buying_power = float(acc.buying_power)
    target_notional = min(equity * TRIAL_BUCKET_PCT, buying_power * 0.95)  # small safety buffer
    if target_notional < price:
        log(f"  insufficient buying power (${buying_power:.2f}) to buy even 1 share of {symbol} @ ${price:.2f} — skipping execution, flagging only")
        return None

    limit_price = round(price * 1.01, 2)  # small buffer to help it fill
    qty = int(target_notional // limit_price)
    if qty < 1:
        return None
    o = api.submit_order(symbol=symbol, qty=qty, side='buy', type='limit',
                          limit_price=limit_price, time_in_force='gtc', extended_hours=True)
    log(f"  ✓ BUY {symbol} qty={qty} limit=${limit_price} order={o.id[:8]}")
    return qty, limit_price, o.id


SATELLITE_PAUSE_FILE = '/home/qbao775/serenity-trader-stack/.SATELLITE_BUYING_PAUSED'


def screen_new_candidates(state, held_symbols):
    if os.path.exists(SATELLITE_PAUSE_FILE):
        log(f"⏸️ 卫星仓建仓已暂停 ({SATELLITE_PAUSE_FILE} 存在) — 跳过新标的筛选的自动买入,"
            f"仅做本地免费分析记录,不会下单。删除该文件以恢复自动建仓。")
        for sym in CANDIDATE_WATCHLIST:
            if sym in held_symbols:
                continue
            ctx = get_candidate_context(sym)
            if ctx and ctx.get('price'):
                append_update(sym, f"### {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC} 新标的筛选(暂停买入)\n"
                                    f"- 行情: 现价 ${ctx['price']:.2f}\n- 建仓已暂停,仅记录,不分析不买入\n")
        return
    for sym in CANDIDATE_WATCHLIST:
        if sym in held_symbols:
            continue
        log(f"── candidate screen: {sym} ──")
        ctx = get_candidate_context(sym)
        if not ctx or not ctx.get('price'):
            log(f"  no quote data for {sym} — skipping")
            continue
        context_str = (f"real-time price ${ctx['price']:.2f} (as of {ctx.get('price_as_of', 'unknown time')}, "
                        f"includes pre/post-market), today's change {ctx.get('change_pct', 0):+.2f}%, "
                        f"52w range ${ctx.get('fifty_two_week_low', 0):.2f}-${ctx.get('fifty_two_week_high', 0):.2f}")

        master_take = quick_4master_new_screen(sym, context_str)
        serenity_take = quick_serenity_new_screen(sym, context_str)
        master_dir = parse_overall(master_take)
        serenity_dir = parse_overall(serenity_take)
        log(f"  4-master: {master_dir}  |  serenity: {serenity_dir}")
        log_verdict(sym, '4master_candidate', master_dir, ctx['price'])
        log_verdict(sym, 'serenity_candidate', serenity_dir, ctx['price'])

        entry = (f"### {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC} 新标的自动筛选\n"
                  f"- 行情: {context_str}\n"
                  f"- 4大师速览: {master_dir}\n{master_take}\n"
                  f"- Serenity速览: {serenity_dir}\n{serenity_take}\n")

        if master_dir == 'BULLISH' and serenity_dir == 'BULLISH':
            log(f"  🔺 两框架一致 BULLISH — 升级付费复核")
            claude_answer, cost = escalate_new_candidate(sym, context_str, master_take, serenity_take)
            entry += f"- **升级触发**: 两框架一致BULLISH\n- **付费深度判断** (${cost:.4f}): {claude_answer}\n"
            if claude_answer and re.search(r'\bBUY\b', claude_answer, re.I) and not re.search(r'\bPASS\b', claude_answer, re.I):
                if abs(ctx.get('change_pct') or 0) > CHASE_GUARD_INTRADAY_PCT:
                    entry += f"- **执行**: 跳过——今日盘中已经 {ctx['change_pct']:+.1f}%,追高违反纪律,等回调\n"
                    log(f"  ⚠️ {sym} 今日 {ctx['change_pct']:+.1f}%,不追高,跳过执行")
                elif not os.path.exists(f'/home/qbao775/serenity-trader-stack/.ENTRY_CONFIRMED_{sym}'):
                    # 2026-07-13: user said "我什么时候让你买你再买" (only buy
                    # when I explicitly tell you to) -- standing policy, applies
                    # to every buy path including this trial-buy candidate
                    # screen, not just SKHY/MU/META. Flag + email, don't execute.
                    entry += f"- **执行**: 达到买入条件,但等待你明确确认——不会自动下单\n"
                    log(f"  🔔 {sym} 达到买入条件,等待用户确认,不自动下单")
                    if not state.setdefault(sym, {}).get('awaiting_confirmation_notified'):
                        state[sym]['awaiting_confirmation_notified'] = True
                        send_email(f"🔔 {sym} 达到买入条件 — 等待你确认",
                                   f"{sym} 试探仓筛选达到买入条件,现价 ~${ctx['price']:.2f}。\n"
                                   f"付费复核意见: {claude_answer[:300]}\n\n"
                                   f"按你的要求,不会自动下单——回复我确认买入,我会执行。")
                else:
                    result = execute_trial_buy(sym, ctx['price'])
                    if result:
                        qty, limit_price, order_id = result
                        entry += f"- **执行**: 试探仓买入 {qty}股 @ ${limit_price} (order {order_id[:8]})\n"
                        state.setdefault(sym, {})['first_bought'] = datetime.datetime.utcnow().isoformat()
                        os.remove(f'/home/qbao775/serenity-trader-stack/.ENTRY_CONFIRMED_{sym}')
                    else:
                        entry += f"- **执行**: 可用资金不足,未下单,仅记录信号\n"
            else:
                entry += f"- **执行**: 付费复核判定 PASS 或不明确,不建仓\n"
        else:
            log(f"  ✓ 未达到一致 BULLISH 门槛,不升级")

        append_update(sym, entry)


VERDICT_LOG = '/home/qbao775/serenity-trader-stack/.verdict_log.jsonl'


def log_verdict(symbol, source, direction, price):
    """Append every gradable (BULLISH/BEARISH) local verdict to a durable log
    so grade_verdicts.py can later check whether the call was actually right.
    Added 2026-07-12 (user: is there a way to improve the algorithm?) --
    up to now the system produced BULLISH/BEARISH calls constantly but never
    checked afterward whether either lens (4-master vs Serenity) was actually
    predictive, so there was no way to tell if the system was getting better
    or worse over time."""
    if direction not in ('BULLISH', 'BEARISH') or not price:
        return
    rec = {'ts': datetime.datetime.utcnow().isoformat(), 'symbol': symbol,
           'source': source, 'direction': direction, 'price_at_verdict': price}
    with open(VERDICT_LOG, 'a') as f:
        f.write(json.dumps(rec) + '\n')


def parse_overall(text):
    m = re.search(r'OVERALL:\s*(BULLISH|NEUTRAL|BEARISH)', text, re.I)
    return m.group(1).upper() if m else 'UNKNOWN'


def parse_chokepoint_intact(text):
    m = re.search(r'CHOKEPOINT_INTACT:\s*(YES|WEAKENING|BROKEN)', text, re.I)
    return m.group(1).upper() if m else 'UNKNOWN'


def escalate_to_claude(symbol, thesis_summary, master_take, serenity_take, reason):
    """PAID call — only invoked when a real trigger fires. Uses `claude -p`
    (the CLI, billed per the user's existing login — NOT a separate API key,
    but genuinely metered; confirmed ~$0.05-0.15/call on 2026-07-03)."""
    prompt = (
        f"卫星仓持仓 {symbol} 触发了自动交叉验证的升级条件: {reason}\n\n"
        f"原始论文: {thesis_summary}\n\n"
        f"本地4大师速览:\n{master_take}\n\n"
        f"本地Serenity速览:\n{serenity_take}\n\n"
        "请你做一次简明的综合判断(不需要完整六步分析,3-5句话即可): "
        "论文是否还成立?本地两个框架的判断有没有道理?给出 HOLD/TRIM/EXIT 的建议。"
    )
    try:
        # cron's minimal PATH doesn't include ~/.local/bin (same class of bug
        # as the mcporter/node fix in news_watch.py) — use the absolute path.
        claude_bin = '/home/qbao775/.local/bin/claude'
        result = subprocess.run(
            [claude_bin, '-p', prompt, '--output-format', 'json'],
            capture_output=True, text=True, timeout=180,
            cwd='/data/qbao775/AlphaTrader'
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            cost = data.get('total_cost_usd', 0)
            answer = data.get('result', '')
            log(f"  claude -p cost: ${cost:.4f}")
            return answer, cost
        else:
            log(f"  claude -p failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"  claude -p exception: {e}")
    return "", 0


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


def get_thesis_summary(symbol):
    """Read the first ~500 chars of the saved thesis file as context."""
    path = f"{REPORTS_DIR}/{symbol}/thesis.md"
    if os.path.exists(path):
        with open(path) as f:
            return f.read()[:800]
    return f"(no saved thesis found for {symbol})"


def append_update(symbol, entry):
    path = f"{REPORTS_DIR}/{symbol}/updates.md"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a') as f:
        f.write(entry + '\n\n')


def main():
    safety_check()
    state = load_state()
    positions = get_satellite_positions()
    escalations = []

    if not positions:
        log("no satellite positions held — nothing to cross-validate")
    else:
        log(f"checking {len(positions)} satellite position(s): {[p['symbol'] for p in positions]}")

    for pos in positions:
        sym = pos['symbol']
        thesis = get_thesis_summary(sym)
        log(f"── {sym} (P&L {pos['unrealized_plpc']:+.1f}%) ──")

        current_price = pos['market_value'] / pos['qty'] if pos['qty'] else 0
        price_context = (f"current price ${current_price:.2f}, cost basis ${pos['cost_basis']:.2f}, "
                          f"qty {pos['qty']}, unrealized P&L {pos['unrealized_plpc']:+.1f}%")

        master_take = quick_4master_take(sym, thesis, price_context)
        serenity_take = quick_serenity_recheck(sym, thesis, price_context)
        master_dir = parse_overall(master_take)
        serenity_dir = parse_overall(serenity_take)
        chokepoint_state = parse_chokepoint_intact(serenity_take)

        log(f"  4-master: {master_dir}  |  serenity: {serenity_dir} (chokepoint: {chokepoint_state})")
        log_verdict(sym, '4master_hold', master_dir, current_price)
        log_verdict(sym, 'serenity_hold', serenity_dir, current_price)

        # Escalation triggers
        reasons = []
        infra_failure = not master_take and not serenity_take
        if infra_failure:
            reasons.append("本地 Ollama 分析失败(两路都返回空)— 无法交叉验证,人工确认模型是否在线")
        if master_dir != 'UNKNOWN' and serenity_dir != 'UNKNOWN' and master_dir != serenity_dir:
            if not (master_dir == 'NEUTRAL' or serenity_dir == 'NEUTRAL'):
                reasons.append(f"两框架分歧 (4大师:{master_dir} vs Serenity:{serenity_dir})")
        if chokepoint_state == 'BROKEN':
            reasons.append("Serenity 判定卡点逻辑已破")
        if master_dir == 'BEARISH' or serenity_dir == 'BEARISH':
            reasons.append(f"出现看空信号")
        if abs(pos['unrealized_plpc']) > PRICE_MOVE_TRIGGER_PCT:
            reasons.append(f"价格大幅波动 ({pos['unrealized_plpc']:+.1f}%)")
        last_deep = state.get(sym, {}).get('last_deepdive')
        if last_deep:
            days_since = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_deep)).days
            if days_since >= STALE_DEEPDIVE_DAYS:
                reasons.append(f"距上次深度复核已 {days_since} 天")
        else:
            reasons.append("从未做过深度复核")

        entry = (f"### {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC} 自动交叉验证\n"
                  f"- P&L: {pos['unrealized_plpc']:+.1f}%\n"
                  f"- 4大师速览: {master_dir}\n{master_take}\n"
                  f"- Serenity速览: {serenity_dir}\n{serenity_take}\n")

        # If the ONLY trigger is the infra failure, and we already paid for
        # this exact "is Ollama down" answer recently, don't pay again —
        # a repeat GPU-contention timeout isn't new information.
        skip_infra_repeat = False
        if infra_failure and len(reasons) == 1:
            last_infra = state.get('_global', {}).get('last_infra_escalation')
            if last_infra:
                hrs_since = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_infra)).total_seconds() / 3600
                if hrs_since < INFRA_FAILURE_COOLDOWN_HOURS:
                    skip_infra_repeat = True
                    log(f"  ⏭️ 本地分析失败,但 {hrs_since:.1f} 小时前已为同样原因付费复核过(冷却期 {INFRA_FAILURE_COOLDOWN_HOURS}h)— 跳过,不重复付费")

        if reasons and not skip_infra_repeat:
            reason_str = "; ".join(reasons)
            log(f"  🔺 升级触发: {reason_str}")
            claude_answer, cost = escalate_to_claude(sym, thesis, master_take, serenity_take, reason_str)
            entry += f"- **升级触发**: {reason_str}\n- **付费深度判断** (${cost:.4f}): {claude_answer}\n"
            escalations.append(f"{sym}: {reason_str}\n  → {claude_answer[:300]}")
            if claude_answer:
                if infra_failure and len(reasons) == 1:
                    state.setdefault('_global', {})['last_infra_escalation'] = datetime.datetime.utcnow().isoformat()
                else:
                    state.setdefault(sym, {})['last_deepdive'] = datetime.datetime.utcnow().isoformat()
            else:
                log(f"  ⚠️ claude -p 深度判断失败,不更新状态(避免误跳过下次复核)")
        elif skip_infra_repeat:
            entry += f"- **升级触发**: {reasons[0]}\n- **跳过付费复核**: 冷却期内({INFRA_FAILURE_COOLDOWN_HOURS}h),避免重复为同一 infra 问题付费\n"
        else:
            log(f"  ✓ 无需升级,本地判断一致且无异常")

        append_update(sym, entry)

    held_symbols = {p['symbol'] for p in positions} | {'SPY', 'QQQ', 'BRK.B'}
    screen_new_candidates(state, held_symbols)

    save_state(state)

    if escalations:
        body = "自动交叉验证发现需要关注的情况:\n\n" + "\n\n".join(escalations)
        send_email(f"🔺 卫星仓交叉验证 — {len(escalations)} 项需关注", body)
        log(f"emailed {len(escalations)} escalation(s)")
    else:
        log("no escalations this cycle — staying silent (no email)")

    # git commit the updates — scoped to this directory only ('.' / '--' pathspec),
    # since reports/ now lives nested inside the much larger AlphaTrader repo
    # (moved 2026-07-07) and a bare `git add -A` would stage the whole repo.
    try:
        subprocess.run(['git', 'add', '-A', '.'], cwd=REPORTS_DIR, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'Auto cross-validate update', '--quiet', '--', '.'],
                       cwd=REPORTS_DIR, capture_output=True)
    except Exception:
        pass


if __name__ == '__main__':
    main()
