#!/usr/bin/env python3
"""
generate_dashboard.py -- builds and deploys a public, read-only dashboard
of the live trading system to Cloudflare Pages.

2026-08-11, user: "可以做一个dashboard后台管理，把持仓的信息同步吗" ->
clarified: "我想要的是所有人可以浏览我的操盘记录和持仓记录还有选股的实时
信息" (public, anyone can browse trade records / positions / real-time
picks), "所以我觉得能在cloudflare pages上部署是最好的", and separately
"仓位多少钱可以不放" (position dollar size doesn't need to be shown).

So: percentage weights and P&L only, never dollar equity/position size --
matches the same privacy policy already applied to README's performance
section and the earlier PII-scrub of this public repo.

Shows:
  1. Performance summary (since-inception vs SPY, all-time net of deposits)
     -- same figures/anchors as update_readme_performance.py.
  2. Current live positions: symbol, weight %, unrealized P&L % (no $).
  3. Today's picks with their catalyst reasons + durability tag, flagging
     which are actually entered vs still pending confirmation.
  4. Recent activity log (last 25 entries from state['action_log']).
  5. Recently exited names still worth watching (from the watch-back
     ledger) -- ties into daily_open_daytrade.py's own watch_back_context_str.

Static site (no live backend) -- regenerated and redeployed by cron via
`wrangler pages deploy`. Not real-time in the truest sense, but as fresh
as the last cron tick (every 15-30 min during market hours is the plan).
"""
import sys
import os
import re
import json
import html
import subprocess
import datetime

sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

STATE_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_state.json'
HISTORY_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_DRYRUN_history.jsonl'
WATCHBACK_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_watchback.jsonl'
LESSONS_FILE = '/home/qbao775/serenity-trader-stack/.daily_open_daytrade_lessons.jsonl'
BUILD_DIR = '/home/qbao775/serenity-trader-stack/dashboard/build'
CF_ENV_FILE = '/home/qbao775/serenity-trader-stack/.secrets/cloudflare.env'
PROJECT_NAME = 'serenity-alphatrader-live'

INCEPTION_EQUITY = 61016.51
INCEPTION_SPY_CLOSE = 754.81
INCEPTION_DATE = '2026-07-16'


def log(msg):
    print(f"[{datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC}] {msg}", flush=True)


def _creds():
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    db.close()
    return k, s


def fetch_live_data():
    import requests
    k, s = _creds()
    h = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}
    acct = requests.get('https://api.alpaca.markets/v2/account', headers=h, timeout=15).json()
    equity = float(acct['equity'])
    positions = requests.get('https://api.alpaca.markets/v2/positions', headers=h, timeout=15).json()
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
        page = requests.get('https://api.alpaca.markets/v2/account/activities', headers=h, params=params, timeout=20).json()
        if not page:
            break
        for a in page:
            amt = float(a.get('net_amount', a.get('amount', 0)))
            net_deposit += -abs(amt) if a['activity_type'] == 'CSW' else abs(amt)
        if len(page) < 100:
            break
        page_token = page[-1]['id']
    all_time_pct = ((equity - net_deposit) / net_deposit * 100) if net_deposit else None

    pos_rows = []
    for p in positions:
        mv = float(p['market_value'])
        pos_rows.append({
            'symbol': p['symbol'],
            'weight_pct': round(mv / equity * 100, 1),
            'plpc': round(float(p['unrealized_plpc']) * 100, 2),
            'day_plpc': round(float(p['unrealized_intraday_plpc']) * 100, 2),
        })
    pos_rows.sort(key=lambda r: -r['weight_pct'])
    return round(sub_pct, 2), round(spy_pct, 2), (round(all_time_pct, 2) if all_time_pct is not None else None), pos_rows


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    return json.load(open(STATE_FILE))


def load_history(n=15):
    if not os.path.exists(HISTORY_FILE):
        return []
    rows = []
    for line in open(HISTORY_FILE).read().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-n:]


def load_watchback(n=10):
    if not os.path.exists(WATCHBACK_FILE):
        return []
    rows = []
    for line in reversed(open(WATCHBACK_FILE).read().splitlines()):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
        if len(rows) >= n:
            break
    return rows


def load_lessons(n=8):
    if not os.path.exists(LESSONS_FILE):
        return []
    out = []
    for line in reversed(open(LESSONS_FILE).read().splitlines()):
        try:
            e = json.loads(line)
        except Exception:
            continue
        for les in e.get('lessons', []):
            out.append((e.get('date'), les))
        if len(out) >= n:
            break
    return out[:n]


def esc(s):
    return html.escape(str(s), quote=True)


def render_html(sub_pct, spy_pct, all_time_pct, pos_rows, state, history, watchback, lessons):
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    lead_word = "领先" if sub_pct >= spy_pct else "落后"

    weights = state.get('weights', {})
    reasons = state.get('reasons', {})
    symbols_state = state.get('symbols', {})
    today_picks_rows = ""
    for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        entered = symbols_state.get(sym, {}).get('entered')
        status = "已建仓" if entered else "待确认(未建仓)"
        reason = reasons.get(sym, "")
        today_picks_rows += (
            f"<tr><td class='sym'>{esc(sym)}</td><td>{w*100:.1f}%</td>"
            f"<td class='status {'ok' if entered else 'pending'}'>{status}</td>"
            f"<td class='reason'>{esc(reason)}</td></tr>\n"
        )

    pos_table_rows = ""
    for r in pos_rows:
        cls = 'pos' if r['plpc'] >= 0 else 'neg'
        pos_table_rows += (
            f"<tr><td class='sym'>{esc(r['symbol'])}</td><td>{r['weight_pct']:.1f}%</td>"
            f"<td class='{cls}'>{r['plpc']:+.2f}%</td><td class='{cls}'>{r['day_plpc']:+.2f}%</td></tr>\n"
        )
    if not pos_table_rows:
        pos_table_rows = "<tr><td colspan='4' class='muted'>当前空仓(资金在现金/美债)</td></tr>"

    history_rows = ""
    for h in reversed(history):
        picks_str = ", ".join(f"{s}({w*100:.0f}%)" for s, w in h.get('weights', {}).items()) or "空仓"
        d_pl = h.get('final_pl_pct')
        spy_p = h.get('spy_pct')
        cls = 'pos' if (d_pl or 0) >= 0 else 'neg'
        d_pl_str = f"{d_pl:+.2f}%" if d_pl is not None else "n/a"
        spy_p_str = f"{spy_p:+.2f}%" if spy_p is not None else "n/a"
        history_rows += (
            f"<tr><td>{esc(h.get('date'))}</td><td class='{cls}'>{d_pl_str}</td>"
            f"<td>{spy_p_str}</td><td class='reason'>{esc(picks_str)}</td></tr>\n"
        )

    action_log = state.get('action_log', [])[-25:]
    log_items = "".join(f"<li>{esc(a)}</li>\n" for a in reversed(action_log)) or "<li class='muted'>今天暂无操作记录</li>"

    wb_rows = ""
    for e in watchback:
        wb_rows += f"<li><span class='sym'>{esc(e.get('symbol'))}</span> {esc(e.get('date'))}卖出 @ ${e.get('exit_price')} — {esc(e.get('reason', ''))[:200]}</li>\n"
    if not wb_rows:
        wb_rows = "<li class='muted'>暂无</li>"

    lesson_items = "".join(f"<li><span class='muted'>({esc(d)})</span> {esc(l)}</li>\n" for d, l in lessons) or "<li class='muted'>暂无</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SerenityAlphaTrader — 实时交易仪表盘</title>
<style>
  :root {{
    --bg: #0b0d12; --panel: #121620; --border: #232a38; --text: #e8ecf3;
    --muted: #8b95a8; --accent: #5b9dff; --pos: #37d67a; --neg: #ff5c72;
    --pending: #f5b942;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.6 -apple-system, "PingFang SC", "Segoe UI", Roboto, "Microsoft YaHei", sans-serif; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 80px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 28px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 32px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }}
  .card .label {{ color: var(--muted); font-size: 12px; letter-spacing: .02em; }}
  .card .value {{ font-size: 24px; font-weight: 600; margin-top: 6px; }}
  section {{ margin-bottom: 36px; }}
  section h2 {{ font-size: 15px; letter-spacing: .02em; color: var(--muted); border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: var(--muted); font-weight: 500; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  td.sym {{ font-weight: 600; }}
  td.reason {{ color: var(--muted); max-width: 480px; }}
  td.pos {{ color: var(--pos); font-weight: 600; }}
  td.neg {{ color: var(--neg); font-weight: 600; }}
  td.status.ok {{ color: var(--pos); }}
  td.status.pending {{ color: var(--pending); }}
  .muted {{ color: var(--muted); }}
  ul.log {{ list-style: none; margin: 0; padding: 0; font-size: 13px; }}
  ul.log li {{ padding: 7px 0; border-bottom: 1px solid var(--border); color: var(--muted); }}
  ul.log li .sym {{ color: var(--text); font-weight: 600; }}
  footer {{ color: var(--muted); font-size: 12px; text-align: center; margin-top: 40px; }}
  a {{ color: var(--accent); }}
  @media (max-width: 640px) {{ td.reason {{ max-width: 220px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>SerenityAlphaTrader — 实时交易仪表盘</h1>
  <div class="sub">自主AI日内交易系统 · 只读展示,不可操作 · 自动刷新 · 最后更新 {now}</div>

  <div class="cards">
    <div class="card"><div class="label">自 {INCEPTION_DATE} 以来</div><div class="value">{sub_pct:+.2f}%</div></div>
    <div class="card"><div class="label">同期SPY</div><div class="value">{spy_pct:+.2f}%</div></div>
    <div class="card"><div class="label">相对SPY</div><div class="value">{lead_word} {abs(sub_pct-spy_pct):.2f}pp</div></div>
    <div class="card"><div class="label">全历史(扣除出入金后)</div><div class="value">{('%+.2f%%' % all_time_pct) if all_time_pct is not None else 'n/a'}</div></div>
  </div>

  <section>
    <h2>当前持仓(仅显示权重和盈亏百分比,不显示具体仓位金额)</h2>
    <table>
      <tr><th>代码</th><th>权重</th><th>浮动盈亏</th><th>今日盈亏</th></tr>
      {pos_table_rows}
    </table>
  </section>

  <section>
    <h2>今日选股({esc(state.get('date', ''))})</h2>
    <table>
      <tr><th>代码</th><th>目标权重</th><th>状态</th><th>催化剂理由</th></tr>
      {today_picks_rows or "<tr><td colspan='4' class='muted'>今天暂无选股</td></tr>"}
    </table>
  </section>

  <section>
    <h2>近期操作记录</h2>
    <ul class="log">
      {log_items}
    </ul>
  </section>

  <section>
    <h2>每日战绩(最近 {len(history)} 个交易日)</h2>
    <table>
      <tr><th>日期</th><th>当日盈亏</th><th>同期SPY</th><th>当日选股</th></tr>
      {history_rows or "<tr><td colspan='4' class='muted'>暂无历史记录</td></tr>"}
    </table>
  </section>

  <section>
    <h2>卖出观察名单(因仓位/风险原因卖出,论文未必破裂,可能值得重新考虑)</h2>
    <ul class="log">{wb_rows}</ul>
  </section>

  <section>
    <h2>近期自我复盘教训</h2>
    <ul class="log">{lesson_items}</ul>
  </section>

  <footer>
    决策支持/研究性质系统,不构成投资建议。源代码: <a href="https://github.com/14H034160212/AlphaTrader">github.com/14H034160212/AlphaTrader</a>
  </footer>
</div>
</body>
</html>
"""


def deploy():
    env = dict(os.environ)
    if os.path.exists(CF_ENV_FILE):
        for line in open(CF_ENV_FILE).read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    r = subprocess.run(
        ['wrangler', 'pages', 'deploy', BUILD_DIR, '--project-name', PROJECT_NAME,
         '--branch', 'main', '--commit-dirty=true'],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if r.returncode != 0:
        log(f"deploy failed: {r.stderr[-500:]}")
        return False
    log(f"deployed: {r.stdout[-300:]}")
    return True


def main():
    try:
        sub_pct, spy_pct, all_time_pct, pos_rows = fetch_live_data()
    except Exception as e:
        log(f"failed to fetch live data: {e}")
        return
    state = load_state()
    history = load_history()
    watchback = load_watchback()
    lessons = load_lessons()

    os.makedirs(BUILD_DIR, exist_ok=True)
    out_path = os.path.join(BUILD_DIR, 'index.html')
    open(out_path, 'w').write(render_html(sub_pct, spy_pct, all_time_pct, pos_rows, state, history, watchback, lessons))
    log(f"rendered dashboard (sub={sub_pct:+.2f}% spy={spy_pct:+.2f}% positions={len(pos_rows)})")
    deploy()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
