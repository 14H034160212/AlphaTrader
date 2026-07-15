#!/usr/bin/env python3
"""
unified_rebalance_20260715.py — ONE-OFF, 2026-07-15 ONLY. User:
"spy/qqq/其他今天利好和涨幅潜力最高的股票动态调仓" (SPY/QQQ/other today's
best-news highest-potential stocks -- dynamic reallocation across ALL of
them, not just within the SPY/QQQ pair).

This generalizes plan_d_daytrade_20260715.py's rebalance_spy_qqq() to the
FULL set of symbols any of today's scripts might be holding: SPY, QQQ, plus
whatever momentum_chase_20260715.py is currently chasing and the 4
news-catalyst names (PYPL/ASML/BABA/MS). Rather than editing the three
already-tested, currently-running scripts right as trading is about to
start (real risk of introducing a bug or a race condition right when timing
matters most), this runs as an INDEPENDENT, ADDITIONAL script on the same
1-minute cadence, purely reading live positions from the API (never
assumes what another script has done) and only acting on a REAL, sustained
gap -- same discipline as the SPY/QQQ-only version, generalized.

Safety notes:
  - Reads positions fresh from Alpaca each tick -- never relies on another
    script's local state file, so it can't get out of sync with what's
    actually held.
  - REBALANCE_COOLDOWN_MIN is longer here (15min, vs the SPY/QQQ-only
    version's 10min) specifically because multiple scripts are now acting
    on overlapping symbols (SPY/QQQ) in the same minute -- a longer
    cooldown reduces the chance of this script and another script's own
    logic fighting over the same shares in the same tick.
  - Only ever trims/adds ONE pair per tick (biggest laggard -> biggest
    leader), not a full multi-way rebalance -- keeps the blast radius of
    any single action small.
  - Needs >=2 of the tracked symbols actually held to do anything.
"""
import sys, os, json, datetime
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

TRACKED_SYMBOLS = ['SPY', 'QQQ', 'PYPL', 'ASML', 'BABA', 'MS']
REBALANCE_GAP_PCT = 2.0       # min plpc divergence before shifting capital (wider than
                               # the SPY/QQQ-only 1.5pp -- this spans more different names)
REBALANCE_SHARE = 0.25        # move this fraction of the laggard's value to the leader
REBALANCE_COOLDOWN_MIN = 15
STATE_FILE = '/home/qbao775/serenity-trader-stack/.unified_rebalance_20260715_state.json'


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


def get_momentum_chase_symbol():
    # momentum_chase_20260715.py may be holding a rotating 5th name -- include
    # it in the comparison too if one exists right now.
    state_file = '/home/qbao775/serenity-trader-stack/.momentum_chase_20260715_state.json'
    if os.path.exists(state_file):
        try:
            st = json.load(open(state_file))
            return st.get('chased_symbol')
        except Exception:
            return None
    return None


def main():
    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    mins_to_close = (clock.next_close - datetime.datetime.now(clock.next_close.tzinfo)).total_seconds() / 60
    if mins_to_close <= 20:
        log("  within 20min of close — leave positions alone for the mandatory close-out to handle")
        return

    state = load_state()
    last_rebalance = state.get('_last_rebalance')
    if last_rebalance:
        elapsed = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_rebalance)).total_seconds() / 60
        if elapsed < REBALANCE_COOLDOWN_MIN:
            log(f"  cooldown active ({elapsed:.0f}/{REBALANCE_COOLDOWN_MIN}min since last rebalance) — skipping")
            return

    symbols = list(TRACKED_SYMBOLS)
    chase_sym = get_momentum_chase_symbol()
    if chase_sym and chase_sym not in symbols:
        symbols.append(chase_sym)

    positions = {p.symbol: p for p in api.list_positions() if p.symbol in symbols}
    if len(positions) < 2:
        log(f"  only {len(positions)} of the tracked symbols currently held — need >=2 to compare, skipping")
        return

    plpc = {sym: float(p.unrealized_plpc) * 100 for sym, p in positions.items()}
    leader = max(plpc, key=plpc.get)
    laggard = min(plpc, key=plpc.get)
    gap = plpc[leader] - plpc[laggard]
    log(f"  holdings: " + ", ".join(f"{s}={v:+.2f}%" for s, v in sorted(plpc.items(), key=lambda kv: -kv[1])))

    if gap < REBALANCE_GAP_PCT:
        log(f"  gap {gap:.2f}pp < {REBALANCE_GAP_PCT}pp threshold — no rebalance needed")
        return

    lag_p = positions[laggard]
    trim_notional = float(lag_p.market_value) * REBALANCE_SHARE
    trim_qty = round(trim_notional / (float(lag_p.market_value) / float(lag_p.qty)), 4)
    if trim_qty <= 0:
        return

    o = api.submit_order(symbol=laggard, qty=trim_qty, side='sell', type='market', time_in_force='day')
    log(f"  ↔ {laggard} ({plpc[laggard]:+.2f}%) lagging {leader} ({plpc[leader]:+.2f}%) by {gap:.2f}pp "
        f"— trimmed {trim_qty}sh order={o.id[:8]}")

    import time
    time.sleep(8)
    acc = api.get_account()
    bp = float(acc.buying_power)
    import market_data as md
    q = md.get_stock_quote(leader)
    px = q['current'] if q and q.get('current') else None
    if px and bp > 20:
        fractionable = leader != 'SKHY'
        add_qty = round((bp - 20) / px, 4) if fractionable else int((bp - 20) / px)
        if add_qty > 0:
            o2 = api.submit_order(symbol=leader, qty=add_qty, side='buy', type='market', time_in_force='day')
            log(f"  ↔ added {add_qty}sh {leader} @~${px:.2f} order={o2.id[:8]}")

    state['_last_rebalance'] = datetime.datetime.utcnow().isoformat()
    state.setdefault('rebalance_log', []).append(
        f"[{datetime.datetime.utcnow().strftime('%H:%M UTC')}] {laggard}({plpc[laggard]:+.2f}%) -> {leader}({plpc[leader]:+.2f}%), gap={gap:.1f}pp")
    save_state(state)


if __name__ == '__main__':
    main()
