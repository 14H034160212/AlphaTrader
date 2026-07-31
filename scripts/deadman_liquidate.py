#!/usr/bin/env python3
"""
Dead-man's-switch failsafe -- runs on GitHub Actions (NOT on the trading
server), so it still works when the trading server is down.

2026-07-31, user: "如果远程服务器关了的话最好全部自动卖掉然后全部买美债"
(if the remote server shuts down, everything should be automatically sold
and parked into treasuries).

Logic:
  1. Read the `heartbeat` branch of this repo -- the trading server
     force-pushes a fresh empty commit there every 5 minutes while alive.
  2. If the latest heartbeat is FRESH (< STALE_MINUTES old): server is
     alive and managing its own positions -- do nothing.
  3. If the heartbeat is STALE and the market is open and the account
     holds any non-SGOV position: liquidate them all at market and park
     the cash into SGOV (notional market order), i.e. the safest state
     for an unmanaged account.
  4. Idempotent by construction: once flat + parked, later runs no-op.

Fail-safe bias: if the heartbeat branch can't be read at all (GitHub API
error / branch missing), we do NOTHING -- a monitoring failure must not
liquidate a healthy account. Only a VALID, readable, stale timestamp
triggers action.
"""
import os
import sys
import time
import datetime
import requests

ALPACA = 'https://api.alpaca.markets'
H = {
    'APCA-API-KEY-ID': os.environ['ALPACA_API_KEY'],
    'APCA-API-SECRET-KEY': os.environ['ALPACA_SECRET_KEY'],
}
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
STALE_MINUTES = int(os.environ.get('STALE_MINUTES', '30'))
REPO = os.environ.get('HEARTBEAT_REPO', '14H034160212/AlphaTrader')


def log(msg):
    print(f"[deadman] {msg}", flush=True)


def main():
    # 1. Heartbeat freshness
    r = requests.get(f'https://api.github.com/repos/{REPO}/branches/heartbeat', timeout=20)
    if r.status_code != 200:
        log(f"cannot read heartbeat branch (HTTP {r.status_code}) -- refusing to act on a monitoring failure")
        return 0
    iso = r.json()['commit']['commit']['committer']['date']
    beat = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    age_min = (datetime.datetime.now(datetime.timezone.utc) - beat).total_seconds() / 60
    log(f"last heartbeat: {iso} ({age_min:.1f} min ago, threshold {STALE_MINUTES} min)")
    if age_min < STALE_MINUTES:
        log("server alive -- nothing to do")
        return 0

    # 2. Market open?
    clock = requests.get(f'{ALPACA}/v2/clock', headers=H, timeout=20).json()
    if not clock.get('is_open'):
        log("server STALE but market is closed -- will act on the next in-hours run")
        return 0

    # 3. Any unmanaged stock positions?
    positions = requests.get(f'{ALPACA}/v2/positions', headers=H, timeout=20).json()
    stocks = [p for p in positions if p['symbol'] != 'SGOV']
    log(f"server STALE, market open. positions: {[(p['symbol'], p['qty']) for p in positions]}")

    if DRY_RUN:
        log(f"DRY_RUN -- would liquidate {[p['symbol'] for p in stocks]} and park cash into SGOV")
        return 0

    for p in stocks:
        resp = requests.delete(f"{ALPACA}/v2/positions/{p['symbol']}", headers=H, timeout=20)
        log(f"liquidate {p['symbol']} qty={p['qty']}: HTTP {resp.status_code}")

    if stocks:
        time.sleep(20)  # let fills settle before reading cash

    # 4. Park all cash into SGOV
    acct = requests.get(f'{ALPACA}/v2/account', headers=H, timeout=20).json()
    cash = float(acct.get('cash', 0))
    if cash > 5:
        order = {'symbol': 'SGOV', 'notional': str(round(cash - 1, 2)),
                 'side': 'buy', 'type': 'market', 'time_in_force': 'day'}
        resp = requests.post(f'{ALPACA}/v2/orders', headers=H, json=order, timeout=20)
        log(f"park ${cash:.2f} into SGOV: HTTP {resp.status_code} {resp.text[:200]}")
    else:
        log(f"cash ${cash:.2f} -- nothing to park")

    log("FAILSAFE EXECUTED: account liquidated to SGOV because the trading server went silent")
    return 0


if __name__ == '__main__':
    sys.exit(main())
