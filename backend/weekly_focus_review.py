#!/usr/bin/env python3
"""
weekly_focus_review.py — weekend self-review of the week's captured signals,
auto-adjusting which tickers get proactive candidate-screening attention.

User request (2026-07-12): "我觉得你应该养成每周末自己回顾一下搜集信息，然后
自动调整应该关注的公司" (develop a habit of reviewing collected info every
weekend and automatically adjusting which companies should be watched).

This does NOT touch held positions, place orders, or change the long-term
SKHY/MU/META holds — it only maintains crossvalidate_satellite.py's
CANDIDATE_WATCHLIST (the proactive new-candidate screen introduced 2026-07-07,
originally just ['EWY']). Per the standing "no manual watchlist" rule
(feedback_no_manual_watchlist.md memory), the list must come from what the
week's data actually surfaced, not from a person hand-picking tickers.

Sources cross-referenced this run (independent of each other, so a ticker
needs signal from >=2 to get added — avoids chasing a single noisy source):
  1. serenity_current_focus.json  — semiconductor-sector chokepoint mention
     ranking (refreshed daily by refresh_serenity_intel.py)
  2. smart_money_signals.json     — Buffett/Congress/influencer-tweet tickers
     (fetch_smart_money.sh, LAGGED cross-check only per feedback_track_
     smart_money.md, but still a legitimate independent data point)
  3. news_watch_seen (DB setting) — tickers extracted from the last ~60
     alerted headlines news_watch.py has actually surfaced this week

Decision rule (deterministic, no LLM in the loop — this is bookkeeping, not
a trading judgment call):
  ADD    a ticker to CANDIDATE_WATCHLIST if it appears in >=2 of the 3
         sources above, is not already on the list, and is not already a
         held position (crossvalidate_satellite.py auto-tracks anything
         held regardless of this list).
  REMOVE a ticker only after it has shown up in ZERO of the 3 sources for
         3 consecutive weekly runs in a row (tracked in the state file) —
         a single quiet week doesn't drop it, avoiding whipsaw.

Writes a dated entry to WEEKLY_FOCUS_LOG.md, emails a Chinese summary, and
commits+pushes the CANDIDATE_WATCHLIST change if one was made.
"""
import sys, os, re, json, datetime, subprocess
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

from database import SessionLocal, get_setting

STACK_DIR = os.path.realpath('/home/qbao775/serenity-trader-stack')  # a symlink into
# the AlphaTrader repo -- git resolves cwd/paths against the realpath repo root, so
# `git add` with the symlinked /home/... prefix fails with "outside repository";
# always use the resolved STACK_DIR for anything passed to git.
SATELLITE_SCRIPT = f'{STACK_DIR}/scripts/crossvalidate_satellite.py'
STATE_FILE = f'{STACK_DIR}/.weekly_focus_state.json'
LOG_MD = f'{STACK_DIR}/WEEKLY_FOCUS_LOG.md'
FOCUS_JSON = '/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit/data/serenity_current_focus.json'
SMART_MONEY_JSON = '/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit/data/smart_money_signals.json'
ABSENT_WEEKS_TO_REMOVE = 3
TOP_FOCUS_N = 15   # only consider the top N of the sector ranking, not the long tail

# Names with their OWN dedicated long-term entry script (skhy_position.py,
# mu_reentry.py, meta_longhold.py — bespoke sizing 20%/5%/8%, no stop-loss,
# watch-and-wait entry) must be excluded here even while NOT YET held (still
# in their own watch phase) -- otherwise this generic 3%-trial-buy candidate
# screen (execute_trial_buy(), TRIAL_BUCKET_PCT) could buy a SECOND, smaller,
# differently-sized position in the same name through a different code path
# while the dedicated script is still watching for its own entry, silently
# conflicting with the user's explicit sizing/no-stop-loss instructions for
# those three names.
DEDICATED_SCRIPT_TICKERS = {'SKHY', 'MU', 'META'}

NAME2TICK = {
    "micron": "MU", "sandisk": "SNDK", "sk hynix": "SKHY", "skhynix": "SKHY",
    "nvidia": "NVDA", "broadcom": "AVGO", "meta": "META", "tsmc": "TSM",
    "coherent": "COHR", "lumentum": "LITE", "arm": "ARM", "intel": "INTC",
}


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def load_json(path):
    try:
        return json.load(open(path))
    except Exception as e:
        log(f"  couldn't load {path}: {e}")
        return {}


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
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: email_sender/email_app_password/email_recipient not set in DB")
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


def extract_tickers_from_headlines(headlines):
    """Best-effort $TICKER / company-name extraction from alerted headline text."""
    found = set()
    for h in headlines:
        for m in re.findall(r"\$([A-Za-z]{1,5})\b", h):
            found.add(m.upper())
        low = h.lower()
        for name, tick in NAME2TICK.items():
            if name in low:
                found.add(tick)
    return found


def read_candidate_watchlist():
    src = open(SATELLITE_SCRIPT).read()
    m = re.search(r"CANDIDATE_WATCHLIST\s*=\s*\[(.*?)\]", src, re.S)
    if not m:
        raise RuntimeError("couldn't find CANDIDATE_WATCHLIST in crossvalidate_satellite.py")
    items = re.findall(r"'([A-Za-z.]+)'|\"([A-Za-z.]+)\"", m.group(1))
    return [a or b for a, b in items]


def write_candidate_watchlist(tickers):
    src = open(SATELLITE_SCRIPT).read()
    new_line = "CANDIDATE_WATCHLIST = [" + ", ".join(f"'{t}'" for t in tickers) + "]"
    new_src = re.sub(r"CANDIDATE_WATCHLIST\s*=\s*\[.*?\]", new_line, src, count=1, flags=re.S)
    with open(SATELLITE_SCRIPT, 'w') as f:
        f.write(new_src)


def main():
    log("weekly focus review starting")

    focus = load_json(FOCUS_JSON)
    top_focus = set(focus.get("top_focus", [])[:TOP_FOCUS_N])
    focus_src_date = focus.get("source_date_hint", "unknown")

    smart_money = load_json(SMART_MONEY_JSON)
    sm_tickers = set(smart_money.get("tickers", []))

    db = SessionLocal()
    seen_raw = get_setting(db, "news_watch_seen", 1, "").split("||")
    db.close()
    news_tickers = extract_tickers_from_headlines([h for h in seen_raw if h])

    api = get_alpaca()
    held = {p.symbol for p in api.list_positions()}

    current_watchlist = read_candidate_watchlist()

    # breadth = how many of the 3 independent sources mention this ticker
    all_tickers = top_focus | sm_tickers | news_tickers
    breadth = {}
    for t in all_tickers:
        breadth[t] = sum([t in top_focus, t in sm_tickers, t in news_tickers])

    state = load_state()

    to_add = [t for t in all_tickers
              if breadth[t] >= 2 and t not in current_watchlist and t not in held
              and t not in DEDICATED_SCRIPT_TICKERS]

    to_remove = []
    for t in current_watchlist:
        if t in all_tickers:
            state[t] = {"absent_weeks": 0}
        else:
            prev = state.get(t, {"absent_weeks": 0})
            prev["absent_weeks"] = prev.get("absent_weeks", 0) + 1
            state[t] = prev
            if prev["absent_weeks"] >= ABSENT_WEEKS_TO_REMOVE:
                to_remove.append(t)

    for t in to_add:
        state[t] = {"absent_weeks": 0}
    for t in to_remove:
        state.pop(t, None)

    new_watchlist = [t for t in current_watchlist if t not in to_remove] + \
                    [t for t in to_add if t not in current_watchlist]
    # keep unique, stable order
    new_watchlist = list(dict.fromkeys(new_watchlist))

    changed = set(new_watchlist) != set(current_watchlist)
    if changed:
        write_candidate_watchlist(new_watchlist)
    save_state(state)

    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    lines = [f"\n## {ts}", f"- 板块聚焦数据来源日期: {focus_src_date}",
             f"- 本周三方信号并集: {len(all_tickers)} 只 (行业聚焦榜前{TOP_FOCUS_N}/聪明钱/新闻提醒)"]
    if to_add:
        lines.append(f"- ➕ 新加入观察名单: {', '.join(to_add)} (至少两个独立信号源同时提到)")
    if to_remove:
        lines.append(f"- ➖ 移出观察名单: {', '.join(to_remove)} (连续{ABSENT_WEEKS_TO_REMOVE}周三方信号源都没再出现)")
    if not to_add and not to_remove:
        lines.append("- 无变化,当前观察名单信号仍然成立")
    lines.append(f"- 当前观察名单: {', '.join(new_watchlist) if new_watchlist else '(空)'}")

    with open(LOG_MD, 'a') as f:
        f.write("\n".join(lines) + "\n")

    body = ("每周自动回顾(仅调整\"候选观察名单\" CANDIDATE_WATCHLIST,不涉及已持仓、不下单):\n\n"
            + "\n".join(l.lstrip("- ") for l in lines[1:]) +
            "\n\n说明: 一个股票要同时被行业聚焦榜/聪明钱信号/新闻提醒里至少两个独立来源提到"
            "才会被加入; 移出则要求连续3周三个来源都没再提及,避免因为单周噪音就换名单。")
    send_email(f"🔭 每周关注名单自动回顾 ({ts})", body)
    log(f"done: +{len(to_add)} -{len(to_remove)} watchlist={new_watchlist}")

    if changed:
        try:
            subprocess.run(['git', 'add', SATELLITE_SCRIPT, LOG_MD],
                            cwd=STACK_DIR, check=True)
            subprocess.run(['git', 'commit', '-m',
                             f"chore: weekly focus review {ts} — watchlist +{to_add} -{to_remove}"],
                            cwd=STACK_DIR, check=True)
            subprocess.run(['git', 'push'], cwd=STACK_DIR, check=True)
            log("committed + pushed watchlist change")
        except subprocess.CalledProcessError as e:
            log(f"git commit/push failed: {e}")
    else:
        # still record the log entry even with no watchlist change
        try:
            subprocess.run(['git', 'add', LOG_MD], cwd=STACK_DIR, check=True)
            if subprocess.run(['git', 'diff', '--cached', '--quiet'],
                               cwd=STACK_DIR).returncode != 0:
                subprocess.run(['git', 'commit', '-m', f"chore: weekly focus review {ts} — no change"],
                                cwd=STACK_DIR, check=True)
                subprocess.run(['git', 'push'], cwd=STACK_DIR, check=True)
        except subprocess.CalledProcessError as e:
            log(f"git commit/push failed: {e}")


if __name__ == '__main__':
    main()
