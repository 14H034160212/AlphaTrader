#!/usr/bin/env python3
"""
daily_retro.py -- automated post-close self-review, every trading day.

2026-08-05, user: "你需要有这样的反思能力，然后自己知道需要什么的话自己给
自己授权" (you need this reflection capability yourself -- and when you know
you need something, authorize yourself). Until now, post-mortems only
happened when the user asked "why did we lose money" -- this makes the
reflection loop self-sustaining:

  1. After each close, feed the day's full action log + P&L + SPY benchmark
     to a claude -p review call.
  2. It must produce 1-3 CONCRETE lessons (e.g. "one-off tariff-refund
     earnings beats don't deserve a position"), not platitudes.
  3. Lessons are appended to a dated ledger, and the stock picker reads the
     recent lessons every morning (see history_context_str) -- so yesterday's
     mistake is literally part of tomorrow's decision context.

Cron: 30 20 * * 1-5  (20:30 UTC, right after the US close).
"""
import sys
import os
import json
import datetime
import subprocess
import requests

sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

CLAUDE_BIN = '/home/qbao775/.local/bin/claude'
STATE_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_state.json'
LESSONS_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_lessons.jsonl'


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


def spy_day_change():
    try:
        k, s = _creds()
        r = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=15)
        snap = r.json()
        prev = snap['prevDailyBar']['c']
        close = snap['dailyBar']['c']
        return round((close - prev) / prev * 100, 2)
    except Exception:
        return None


def main():
    if not os.path.exists(STATE_FILE):
        return
    state = json.load(open(STATE_FILE))
    today = state.get('date', '')
    # don't re-review the same day twice
    if os.path.exists(LESSONS_FILE):
        for line in open(LESSONS_FILE).read().splitlines():
            try:
                if json.loads(line).get('date') == today:
                    log(f"retro for {today} already recorded")
                    return
            except Exception:
                pass

    actions = state.get('action_log', [])
    final_pl = state.get('final_pl_pct')
    held_over = state.get('hold_overnight') or bool(state.get('sim_positions'))
    spy = spy_day_change()

    positions_note = ""
    if state.get('sim_positions'):
        import copy
        lines = [f"{s2}: 入价${p['entry_price']}" for s2, p in state['sim_positions'].items()]
        positions_note = "收盘时仍持有(隔夜): " + ", ".join(lines)

    prompt = (
        f"你是一个自动交易系统的复盘教练。下面是{today}(美股交易日)的完整操作记录。"
        "请做诚实的复盘,好的和坏的都必须吸取经验:\n"
        "1. 点名分析今天/当前组合中表现最好的一只:它的催化剂属于哪种类型、入场时机"
        "有什么特征、当时给的权重是否配得上它的表现——赢家的成功模式必须被提炼成"
        "明天可以主动复制的规律(比如'具体金额+长期限的独占合同类催化,弹性最高,"
        "应给最高档权重')。\n"
        "2. 点名分析表现最差的一只:错在哪一类判断(催化剂质量?权重分配?入场时机?"
        "该砍没砍?)。\n"
        "3. 检查当前仍持有的每只standing仓位:它的入场理由是否与教训清单里的任何一条"
        "冲突?如果冲突,明天的持仓判断应该优先处理它。\n"
        "4. 检查资金分布:最强的仓位是不是反而权重最小?是否应该向已验证的赢家集中?\n\n"
        f"当日账户盈亏: {final_pl if final_pl is not None else '未平仓(浮动)'}%\n"
        f"当日大盘SPY: {spy if spy is not None else '未知'}%\n"
        f"{positions_note}\n"
        f"操作记录:\n" + "\n".join(actions[-40:]) + "\n\n"
        "输出要求: 先写复盘分析(覆盖上面4点);然后输出1-4条教训,每条独立一行、以 LESSON: 开头,"
        "必须具体可执行、直接影响明天的选股或持仓判断——赢家规律和输家教训都算"
        "(例如 'LESSON: 一次性退税驱动的利润超预期不构成买入理由' 或 "
        "'LESSON: FTK式独占长期合同催化是最高弹性类型,应给最高档权重')。"
        "严禁空话(如'要更谨慎'、'要控制风险')。"
        "如果今天没有值得记的教训(操作都合理),输出 LESSON: 无。"
    )
    try:
        result = subprocess.run([CLAUDE_BIN, '-p', prompt, '--output-format', 'json'],
                                 capture_output=True, text=True, timeout=180,
                                 cwd='/data/qbao775/AlphaTrader')
        if result.returncode != 0:
            log(f"retro claude -p failed: {result.stderr[:200]}")
            return
        data = json.loads(result.stdout)
        answer = data.get('result', '').strip()
        log(f"retro (cost ${data.get('total_cost_usd', 0):.4f}):\n{answer}")
    except Exception as e:
        log(f"retro exception: {e}")
        return

    lessons = [l.split('LESSON:', 1)[1].strip() for l in answer.splitlines() if 'LESSON:' in l]
    lessons = [l for l in lessons if l and l != '无']
    entry = {'date': today, 'day_pl_pct': final_pl, 'spy_pct': spy,
             'held_overnight': held_over, 'lessons': lessons,
             'analysis': answer[:1500]}
    with open(LESSONS_FILE, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log(f"recorded {len(lessons)} lesson(s) for {today}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
