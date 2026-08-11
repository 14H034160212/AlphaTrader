#!/usr/bin/env python3
"""
update_readme_performance.py -- keeps README.md's Live Performance section
current.

2026-08-11, user: "可以把我们这个代码仓库实时的收益率放在readme里" (put this
repo's real-time returns in the README), clarified with "仓位多少钱可以不放"
(the position's dollar amount doesn't need to be shown) -- so this reports
PERCENTAGE returns only, never equity/position size, keeping the account's
actual scale private on a public repo.

Two figures:
  1. Since the current live system's inception (2026-07-16): account %
     return vs SPY % return over the same window (the two anchors already
     used by daily_open_daytrade.py's own daily summary email).
  2. All-time, net of deposits/withdrawals (CSD/CSW/JNLC activities): true
     performance since the account's very first trade, any era, any
     strategy -- the same honest, deposit-adjusted number reported
     conversationally when asked "how's AlphaTrader done overall".

Only rewrites/commits/pushes README.md when the rounded percentages
actually changed since the last run (stored in a small state file) --
avoids spamming git history with commits that only bump a timestamp.

Cron: every 30 min during market hours, e.g. */30 13-20 * * 1-5.
"""
import sys
import os
import re
import json
import subprocess
import datetime

sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

REPO_DIR = '/data/qbao775/AlphaTrader'
README_PATH = f'{REPO_DIR}/README.md'
STATE_FILE = '/tmp/.readme_performance_state.json'

INCEPTION_EQUITY = 61016.51
INCEPTION_SPY_CLOSE = 754.81
INCEPTION_DATE = '2026-07-16'


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def _creds():
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    return k, s


def compute_performance():
    import requests
    k, s = _creds()
    h = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}

    acct = requests.get('https://api.alpaca.markets/v2/account', headers=h, timeout=15).json()
    equity = float(acct['equity'])

    spy = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot', headers=h, timeout=15).json()
    spy_last = spy.get('latestTrade', {}).get('p') or spy['prevDailyBar']['c']

    sub_pct = (equity - INCEPTION_EQUITY) / INCEPTION_EQUITY * 100
    spy_pct = (spy_last - INCEPTION_SPY_CLOSE) / INCEPTION_SPY_CLOSE * 100

    net_deposit = 0.0
    page_token = None
    while True:
        params = {'activity_types': 'CSD,CSW,JNLC', 'page_size': 100}
        if page_token:
            params['page_token'] = page_token
        r = requests.get('https://api.alpaca.markets/v2/account/activities', headers=h, params=params, timeout=20)
        page = r.json()
        if not page:
            break
        for a in page:
            amt = float(a.get('net_amount', a.get('amount', 0)))
            if a['activity_type'] == 'CSW':
                net_deposit -= abs(amt)
            else:
                net_deposit += abs(amt)
        if len(page) < 100:
            break
        page_token = page[-1]['id']

    all_time_pct = ((equity - net_deposit) / net_deposit * 100) if net_deposit else None
    return round(sub_pct, 2), round(spy_pct, 2), (round(all_time_pct, 2) if all_time_pct is not None else None)


def render_section(sub_pct, spy_pct, all_time_pct):
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    lead = "领先" if sub_pct >= spy_pct else "落后"
    lines = [
        "<!-- PERFORMANCE:START -->",
        f"**Since {INCEPTION_DATE} (current live system):** Account **{sub_pct:+.2f}%** vs SPY **{spy_pct:+.2f}%** "
        f"({lead}大盘 {abs(sub_pct - spy_pct):.2f}pp)",
    ]
    if all_time_pct is not None:
        lines.append(f"**All-time, net of all deposits/withdrawals:** **{all_time_pct:+.2f}%**")
    lines.append(f"\n_Last updated: {now} — percentage returns only, position size/equity not disclosed._")
    lines.append("<!-- PERFORMANCE:END -->")
    return "\n".join(lines)


def main():
    try:
        sub_pct, spy_pct, all_time_pct = compute_performance()
    except Exception as e:
        log(f"failed to compute performance: {e}")
        return

    prev = {}
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE))
        except Exception:
            prev = {}
    if prev.get('sub_pct') == sub_pct and prev.get('spy_pct') == spy_pct and prev.get('all_time_pct') == all_time_pct:
        log("performance unchanged since last check, skipping README update")
        return

    new_section = render_section(sub_pct, spy_pct, all_time_pct)
    content = open(README_PATH).read()
    pattern = re.compile(r'<!-- PERFORMANCE:START -->.*?<!-- PERFORMANCE:END -->', re.DOTALL)
    if pattern.search(content):
        new_content = pattern.sub(new_section, content)
    else:
        lines = content.splitlines()
        insert_at = 0
        for i, l in enumerate(lines):
            if l.startswith('# '):
                insert_at = i + 1
                break
        lines.insert(insert_at, "\n## \U0001F4C8 Live Performance\n\n" + new_section + "\n")
        new_content = "\n".join(lines)

    open(README_PATH, 'w').write(new_content)
    json.dump({'sub_pct': sub_pct, 'spy_pct': spy_pct, 'all_time_pct': all_time_pct}, open(STATE_FILE, 'w'))

    subprocess.run(['git', 'add', 'README.md'], cwd=REPO_DIR, capture_output=True)
    r = subprocess.run(['git', 'commit', '-m', 'Auto-update live performance in README', '--quiet', '--', 'README.md'],
                        cwd=REPO_DIR, capture_output=True, text=True)
    if r.returncode == 0:
        push = subprocess.run(['git', 'push', 'origin', 'main', '--quiet'], cwd=REPO_DIR, capture_output=True, text=True)
        if push.returncode == 0:
            log(f"updated + pushed: sub={sub_pct:+.2f}% spy={spy_pct:+.2f}% all_time={all_time_pct}")
        else:
            log(f"committed but push failed: {push.stderr[:200]}")
    else:
        log(f"git commit failed or nothing to commit: {r.stderr[:200]}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
