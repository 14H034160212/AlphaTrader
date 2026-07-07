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
USER_EMAIL = 'bqmbill714@gmail.com'


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


def ollama_call(prompt, timeout=120):
    try:
        import requests
        r = requests.post(f"{OLLAMA_HOST}/api/generate",
                          json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                          timeout=timeout)
        if r.status_code == 200:
            return r.json().get('response', '').strip()
    except Exception as e:
        log(f"ollama_call failed: {e}")
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
    key = os.environ.get('RESEND_API_KEY')
    if not key:
        log("email skipped: RESEND_API_KEY not set in env")
        return
    try:
        import requests
        r = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={'from': 'onboarding@resend.dev', 'to': [USER_EMAIL],
                  'subject': subject, 'text': body}, timeout=15)
        log(f"email: {r.status_code}")
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

    if not positions:
        log("no satellite positions held — nothing to cross-validate")
        return

    log(f"checking {len(positions)} satellite position(s): {[p['symbol'] for p in positions]}")
    escalations = []

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

        # Escalation triggers
        reasons = []
        if not master_take and not serenity_take:
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

        if reasons:
            reason_str = "; ".join(reasons)
            log(f"  🔺 升级触发: {reason_str}")
            claude_answer, cost = escalate_to_claude(sym, thesis, master_take, serenity_take, reason_str)
            entry += f"- **升级触发**: {reason_str}\n- **付费深度判断** (${cost:.4f}): {claude_answer}\n"
            escalations.append(f"{sym}: {reason_str}\n  → {claude_answer[:300]}")
            if claude_answer:
                state.setdefault(sym, {})['last_deepdive'] = datetime.datetime.utcnow().isoformat()
            else:
                log(f"  ⚠️ claude -p 深度判断失败,不更新 last_deepdive(避免误跳过下次复核)")
        else:
            log(f"  ✓ 无需升级,本地判断一致且无异常")

        append_update(sym, entry)

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
