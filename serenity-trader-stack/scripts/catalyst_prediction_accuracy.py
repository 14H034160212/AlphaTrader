#!/usr/bin/env python3
"""
catalyst_prediction_accuracy.py -- score daily_open_daytrade.py's own catalyst
picks against what the stock actually did afterward.

2026-08-07, user: "关键是你要获取真正准确和有用的信息，然后股价确实是可以
根据这个信息预测的，这个能力你需要不断增强和反思" (the real edge is accurate,
useful information -- prices genuinely are predictable from it -- and that
capability needs continuous strengthening and reflection).

Gap this closes: PREDICTION_ACCURACY.md already does this for the OLDER
Serenity/4-master long-horizon holds, but nothing scored the actual
catalyst reasons pick_todays_stocks() writes into
.daily_open_daytrade_DRYRUN_history.jsonl (e.g. "TEL: 三季度营收/EPS双双超
预期..."). Without this, the catalyst-durability judgments feeding the
picker and daily_retro.py are never checked against reality -- they can
sound plausible and still be wrong, and nothing would notice.

Logic: for each historical pick at least MIN_AGE_DAYS old, compare the
close on the pick date to the close MIN_AGE_DAYS trading days later (or
latest available close). >=+3% = CORRECT, <=-3% = WRONG, else INCONCLUSIVE.
Dedup via a small state file so re-runs don't rescore the same pick.

Cron: run once daily after close, e.g. 35 20 * * 1-5 (after daily_retro.py).
"""
import sys
import os
import json
import datetime
import requests

sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

HISTORY_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_history.jsonl'
LEDGER_FILE = '/home/qbao775/serenity-trader-stack/CATALYST_PREDICTION_ACCURACY.md'
SCORED_STATE = '/home/qbao775/serenity-trader-stack/.catalyst_prediction_scored.json'
MIN_AGE_DAYS = 5          # only score picks at least this many calendar days old
CORRECT_THRESHOLD = 3.0   # %
WRONG_THRESHOLD = -3.0    # %
SKIP_SYMBOLS = {'SPY', 'SGOV'}  # baseline/parking positions, not catalyst picks


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


def daily_bars(sym, start, end, headers):
    r = requests.get(f'https://data.alpaca.markets/v2/stocks/{sym}/bars',
                      params={'start': start, 'end': end, 'timeframe': '1Day', 'feed': 'iex'},
                      headers=headers, timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get('bars', [])


def load_scored():
    if os.path.exists(SCORED_STATE):
        return set(json.load(open(SCORED_STATE)))
    return set()


def save_scored(scored):
    json.dump(sorted(scored), open(SCORED_STATE, 'w'))


def main():
    if not os.path.exists(HISTORY_FILE):
        log("no history file yet")
        return
    scored = load_scored()
    today = datetime.date.today()
    k, s = _creds()
    headers = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}

    to_score = []
    for line in open(HISTORY_FILE).read().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        date_str = d.get('date')
        reasons = d.get('reasons') or {}
        if not date_str or not reasons:
            continue
        try:
            pick_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            continue
        if (today - pick_date).days < MIN_AGE_DAYS:
            continue
        for sym, reason in reasons.items():
            if sym in SKIP_SYMBOLS:
                continue
            key = f"{date_str}|{sym}"
            if key in scored:
                continue
            to_score.append((date_str, pick_date, sym, reason))

    if not to_score:
        log("nothing new to score")
        return

    results = []
    for date_str, pick_date, sym, reason in to_score:
        start = pick_date.isoformat()
        end = min(pick_date + datetime.timedelta(days=14), today).isoformat()
        bars = daily_bars(sym, start, end, headers)
        if len(bars) < 2:
            log(f"  {sym} ({date_str}): insufficient bar data, skipping (will retry later)")
            continue
        entry_close = bars[0]['c']
        # use the bar MIN_AGE_DAYS-ish out if available, else the latest we have
        target_idx = min(MIN_AGE_DAYS, len(bars) - 1)
        exit_close = bars[target_idx]['c']
        pct = round((exit_close - entry_close) / entry_close * 100, 1)
        if pct >= CORRECT_THRESHOLD:
            verdict = 'CORRECT'
            icon = '✅'
        elif pct <= WRONG_THRESHOLD:
            verdict = 'WRONG'
            icon = '❌'
        else:
            verdict = 'INCONCLUSIVE'
            icon = '➖'
        results.append({
            'date': date_str, 'symbol': sym, 'reason': reason,
            'entry_close': entry_close, 'exit_close': exit_close,
            'pct': pct, 'verdict': verdict, 'icon': icon,
        })
        scored.add(f"{date_str}|{sym}")

    if not results:
        log("no scoreable results this run")
        return

    save_scored(scored)

    lines = [f"\n## {today.isoformat()} 评分 {len(results)} 条 (催化剂选股, 入场后{MIN_AGE_DAYS}个交易日)"]
    for r in results:
        lines.append(
            f"  {r['icon']} {r['symbol']} @ ${r['entry_close']} -> ${r['exit_close']} "
            f"({r['pct']:+.1f}%) = {r['verdict']} | {r['reason'][:80]}"
        )
    # rolling accuracy across the whole ledger's scored history
    all_scored_verdicts = []
    if os.path.exists(LEDGER_FILE):
        for l in open(LEDGER_FILE).read().splitlines():
            for tag in ('CORRECT', 'WRONG'):
                if f"= {tag}" in l:
                    all_scored_verdicts.append(tag)
    all_scored_verdicts += [r['verdict'] for r in results if r['verdict'] != 'INCONCLUSIVE']
    n_correct = all_scored_verdicts.count('CORRECT')
    n_total = len(all_scored_verdicts)
    if n_total:
        lines.append(f"- 累计催化剂选股准确率(排除INCONCLUSIVE): {n_correct}/{n_total} = {n_correct/n_total*100:.0f}%")

    with open(LEDGER_FILE, 'a') as f:
        f.write("\n".join(lines) + "\n")
    log(f"scored {len(results)} new pick(s), appended to {LEDGER_FILE}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
