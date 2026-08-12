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
    # 2026-08-12, user: "加上账户金额" -- explicitly asked to add the TOTAL
    # account equity to the public page. Different decision from the earlier
    # "仓位多少钱可以不放" (per-position dollar size stays hidden) -- that
    # policy is unchanged, only the aggregate total is now shown.
    return (round(sub_pct, 2), round(spy_pct, 2),
            (round(all_time_pct, 2) if all_time_pct is not None else None), pos_rows, round(equity, 2))


def fetch_full_track_record():
    """2026-08-11, user: '可以把完整的走势图加上吗？只有近9个工作日肯定不够'
    (the chart only had ~9 points from the thin JSONL history file -- add the
    FULL trend). Alpaca's portfolio/history endpoint has real daily equity
    back to the account's actual trading start (~2026-05-12; before that
    equity is 0.0, i.e. no real activity, so period=3M already covers the
    whole real history). Its 'profit_loss' field is a raw day-over-day equity
    delta -- NOT deposit-adjusted, so it spikes hugely on funding days
    (confirmed: +910% on 2026-07-02, the day after the $55,669 deposit
    landed). Subtract each bar's net deposits/withdrawals (via the CSD/CSW/
    JNLC activity feed) before computing that day's return, so the compounded
    curve reflects trading performance only, matching the 'all-time net of
    deposits' stat card's honesty policy. Returns rows in the same shape
    render_trend_chart() already consumes ({'date','final_pl_pct','spy_pct'})
    so the chart's compounding logic needs no changes."""
    import requests
    k, s = _creds()
    h = {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}
    r = requests.get('https://api.alpaca.markets/v2/account/portfolio/history',
                      headers=h, params={'period': '3M', 'timeframe': '1D'}, timeout=20).json()
    ts = r.get('timestamp') or []
    equity = r.get('equity') or []
    pl = r.get('profit_loss') or []
    if len(ts) < 2:
        return []
    dates = [datetime.datetime.utcfromtimestamp(t).strftime('%Y-%m-%d') for t in ts]

    deposits_by_date = {}
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
            signed = -abs(amt) if a['activity_type'] == 'CSW' else abs(amt)
            deposits_by_date[a['date']] = deposits_by_date.get(a['date'], 0.0) + signed
        if len(page) < 100:
            break
        page_token = page[-1]['id']

    # SPY daily closes over the same window, feed=iex (free-tier). Deposits
    # don't apply here -- SPY closes are absolute prices, no cash-flow noise.
    start = f"{dates[0]}T00:00:00Z"
    end = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    bars = requests.get('https://data.alpaca.markets/v2/stocks/SPY/bars', headers=h,
                         params={'start': start, 'end': end, 'timeframe': '1Day', 'feed': 'iex', 'limit': 1000},
                         timeout=20).json().get('bars', [])
    spy_seq = [(b['t'][:10], b['c']) for b in bars]
    if not spy_seq or spy_seq[-1][0] != dates[-1]:
        try:
            snap = requests.get('https://data.alpaca.markets/v2/stocks/SPY/snapshot', headers=h, timeout=15).json()
            today_px = snap.get('latestTrade', {}).get('p') or snap['prevDailyBar']['c']
            spy_seq.append((dates[-1], today_px))
        except Exception:
            pass

    # 2026-08-11: portfolio_history's own date labels are unreliable -- some
    # bars land on a Saturday when converted (an Alpaca timestamp-anchor
    # quirk, confirmed empirically), which breaks date-string matching
    # against SPY's bars (which use the correct NYSE calendar date) for
    # ~1-in-5 rows. Both sequences cover the SAME set of trading sessions in
    # the SAME chronological order though (verified: 62 SPY bars + today's
    # snapshot == 63 portfolio bars) -- so align positionally and use SPY's
    # reliably-labeled dates for display instead of portfolio_history's.
    use_positional = len(spy_seq) == len(dates)
    if not use_positional:
        log(f"full_track_record: spy_seq len {len(spy_seq)} != portfolio dates len {len(dates)}, "
            f"falling back to by-date matching (may miss some SPY moves)")
        spy_close_by_date = dict(spy_seq)

    rows = []
    last_spy_close = spy_seq[0][1] if use_positional else spy_close_by_date.get(dates[0])
    for i in range(1, len(dates)):
        prev_d, cur_d = dates[i - 1], dates[i]
        # A deposit dated D lands in the equity snapshot of the NEXT trading
        # bar (confirmed empirically: the 07-01 deposit shows as the 07-02
        # bar's jump) -- so the window is [prev bar's date, this bar's date).
        window = sum(v for d, v in deposits_by_date.items() if prev_d <= d < cur_d)
        prev_eq = equity[i - 1]
        acc_ret = ((pl[i] - window) / prev_eq * 100) if prev_eq else 0.0

        if use_positional:
            display_date, spy_close = spy_seq[i]
        else:
            display_date = cur_d
            spy_close = spy_close_by_date.get(cur_d)
        if spy_close is not None and last_spy_close:
            spy_ret = (spy_close - last_spy_close) / last_spy_close * 100
        else:
            spy_ret = 0.0
        if spy_close is not None:
            last_spy_close = spy_close

        rows.append({
            'date': display_date, 'final_pl_pct': round(acc_ret, 4), 'spy_pct': round(spy_ret, 4),
            # 2026-08-12, user asked the chart's hover to show "总金额的浮动
            # 变化" (the fluctuation of the total dollar amount) -- carry the
            # day's actual equity level and its deposit-adjusted dollar
            # change (same adjustment as acc_ret, so the $ figure agrees
            # with the % already shown) alongside the percentages.
            'equity': round(equity[i], 2), 'day_change_usd': round(pl[i] - window, 2),
        })
    return rows


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


# 2026-08-11: user asked for a Chinese company name next to every ticker.
# Manual names for the ones actually seen in this account's history so far
# (accurate, not machine-translated); anything new falls back to the
# English longName from yfinance rather than guessing a Chinese name.
TICKER_NAMES_ZH = {
    'SPY': '标普500ETF', 'SGOV': '短期美债ETF',
    'RKLB': '火箭实验室', 'FTK': '弗洛泰克工业', 'INSM': '因斯梅德',
    'MLTX': '月湖免疫治疗', 'DDOG': 'Datadog(云监控软件)',
    'GRAL': 'GRAIL(癌症早筛)', 'TEM': 'Tempus AI(医疗数据)',
    'CXW': 'CoreCivic(监狱运营商)', 'HII': '亨廷顿英格尔斯工业(造船)',
    'NOC': '诺斯罗普·格鲁曼(军工)', 'CROX': '卡骆驰(鞋类)',
    'COLM': '哥伦比亚服饰', 'ABT': '雅培', 'NVS': '诺华制药',
    'BABA': '阿里巴巴', 'RDY': '太阳药业(Dr. Reddy\'s)',
    'DBD': 'Diebold Nixdorf(自助设备)', 'SUPN': 'Supernus制药',
    'CRI': '卡特斯(童装)', 'AAOI': '应用光电', 'COHR': 'Coherent(光电元件)',
    'LITE': 'Lumentum(光通信)', 'CRDO': 'Credo(光互连芯片)',
    'GLW': '康宁', 'MCHP': '微芯科技', 'GFS': '格芯',
    'SPCX': 'SpaceX(太空探索技术)', 'NTRA': 'Natera(基因检测)',
    'HALO': 'Halozyme制药', 'ABNB': 'Airbnb爱彼迎',
    'TWLO': 'Twilio', 'TEAM': 'Atlassian',
}
_name_cache = {}


def ticker_name_zh(sym):
    if sym in TICKER_NAMES_ZH:
        return TICKER_NAMES_ZH[sym]
    if sym in _name_cache:
        return _name_cache[sym]
    name = ""
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info
        name = info.get('longName') or info.get('shortName') or ""
    except Exception:
        pass
    _name_cache[sym] = name
    return name


def sym_label(sym):
    zh = ticker_name_zh(sym)
    return f"{esc(sym)} <span class='zh'>{esc(zh)}</span>" if zh else esc(sym)


# Chart colors validated via the dataviz skill's validate_palette.js against
# the dashboard's dark surface (#1a1a19): both PASS lightness/chroma/CVD/
# contrast checks. Fixed categorical assignment -- account is always slot 1,
# SPY is always slot 2, never reassigned/cycled.
CHART_COLOR_ACCOUNT = "#3987e5"
CHART_COLOR_SPY = "#d95926"


def render_trend_chart(history, marker_date=None, marker_label=None):
    """Cumulative % return line chart (account vs SPY) compounding each day's
    final_pl_pct/spy_pct chronologically -- the same figure as the 'since
    inception' stat card, but shown as a curve over time instead of one
    endpoint number. Self-contained inline SVG (no JS chart library) so it
    survives as a static Cloudflare Pages asset.

    2026-08-11, user (looking at the full-history chart): '我不理解为什么从
    走势上看我们的走势明显更弱，但是你说我们的收益率超过标普500' -- the full
    history includes an earlier, since-discontinued trading approach (pre
    daily_open_daytrade.py, before 2026-07-16) that lost money and drags the
    whole-history line well below SPY, while the CURRENT system (since
    07-16, what the 'since inception' stat card measures) has actually been
    beating SPY. Both numbers are true for their own window -- the two
    looked contradictory only because the chart gave no visual cue that a
    strategy change happened partway through. marker_date/marker_label draw
    that regime-change line so the discontinuity is self-explanatory instead
    of something the user has to ask about."""
    rows = [h for h in history if h.get('final_pl_pct') is not None and h.get('spy_pct') is not None]
    if len(rows) < 2:
        return ""

    acc_cum, spy_cum = [], []
    a, s = 1.0, 1.0
    for h in rows:
        a *= (1 + h['final_pl_pct'] / 100)
        s *= (1 + h['spy_pct'] / 100)
        acc_cum.append((a - 1) * 100)
        spy_cum.append((s - 1) * 100)

    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 900, 280, 46, 90, 16, 28
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    all_vals = acc_cum + spy_cum + [0.0]
    v_min, v_max = min(all_vals), max(all_vals)
    v_span = (v_max - v_min) or 1.0
    v_min -= v_span * 0.08
    v_max += v_span * 0.08
    v_span = v_max - v_min

    n = len(rows)
    def x_at(i):
        return PAD_L + (i / (n - 1)) * plot_w if n > 1 else PAD_L
    def y_at(v):
        return PAD_T + (1 - (v - v_min) / v_span) * plot_h

    def line_path(vals):
        return "M " + " L ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(vals))

    def points(vals, label_fn):
        return "".join(
            f"<circle cx='{x_at(i):.1f}' cy='{y_at(v):.1f}' r='3.5' fill='{label_fn}'/>"
            for i, v in enumerate(vals)
        )

    dates = [h.get('date', '') for h in rows]
    equities = [h.get('equity') for h in rows]
    day_changes = [h.get('day_change_usd') for h in rows]
    zero_y = y_at(0.0)

    # x-axis date ticks: first, middle, last (avoid label crowding for long histories)
    tick_idxs = sorted(set([0, n // 2, n - 1]))
    x_ticks = "".join(
        f"<text x='{x_at(i):.1f}' y='{H-8}' class='axis-lab' text-anchor='middle'>{esc(dates[i])}</text>"
        for i in tick_idxs
    )
    y_ticks = "".join(
        f"<text x='{PAD_L-8}' y='{y_at(v)+4:.1f}' class='axis-lab' text-anchor='end'>{v:+.0f}%</text>"
        for v in [v_min + v_span * f for f in (0.1, 0.5, 0.9)]
    )

    marker_svg = ""
    if marker_date and marker_date in dates:
        mi = dates.index(marker_date)
        mx = x_at(mi)
        marker_svg = (
            f"<line x1='{mx:.1f}' y1='{PAD_T}' x2='{mx:.1f}' y2='{PAD_T+plot_h}' class='regime-line'/>"
            f"<text x='{mx:.1f}' y='{PAD_T-4}' class='regime-lab' text-anchor='middle'>{esc(marker_label or marker_date)}</text>"
        )

    # 2026-08-12, user: "可以支持鼠标选择可以查看具体某个时段的情况" + "总
    # 金额的浮动变化" -- add a crosshair+tooltip hover layer (per the dataviz
    # skill's interaction spec: a vertical hairline snaps to the nearest x,
    # one tooltip lists every series at that point) showing both series' %
    # AND the account's actual dollar equity/day-change at the hovered date,
    # not just the % the static chart already showed. Values are also fully
    # reachable without hovering, via the daily track-record table below the
    # chart -- the hover layer is a faster path to the same numbers, not the
    # only path (dataviz: "tooltips enhance, they never gate").
    point_data = json.dumps([
        {'d': dates[i], 'a': round(acc_cum[i], 2), 's': round(spy_cum[i], 2),
         'eq': equities[i], 'chg': day_changes[i]}
        for i in range(n)
    ], ensure_ascii=False)

    return f"""
    <div class="trend-chart-wrap" id="trendChartWrap">
      <svg viewBox="0 0 {W} {H}" role="img" aria-label="累计收益走势: 账户 vs SPY" class="trend-chart" id="trendChartSvg">
        <line x1="{PAD_L}" y1="{zero_y:.1f}" x2="{W-PAD_R}" y2="{zero_y:.1f}" class="zero-line"/>
        {y_ticks}
        {x_ticks}
        {marker_svg}
        <path d="{line_path(spy_cum)}" fill="none" stroke="{CHART_COLOR_SPY}" stroke-width="2"/>
        <path d="{line_path(acc_cum)}" fill="none" stroke="{CHART_COLOR_ACCOUNT}" stroke-width="2"/>
        {points(spy_cum, CHART_COLOR_SPY)}
        {points(acc_cum, CHART_COLOR_ACCOUNT)}
        <text x="{x_at(n-1)+8:.1f}" y="{y_at(acc_cum[-1])+4:.1f}" class="end-label" fill="{CHART_COLOR_ACCOUNT}">{acc_cum[-1]:+.1f}%</text>
        <text x="{x_at(n-1)+8:.1f}" y="{y_at(spy_cum[-1])+4:.1f}" class="end-label" fill="{CHART_COLOR_SPY}">{spy_cum[-1]:+.1f}%</text>
        <line id="tcCrosshair" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T+plot_h}" class="crosshair-line" style="opacity:0"/>
        <circle id="tcDotAcc" r="5" fill="{CHART_COLOR_ACCOUNT}" class="hover-dot" style="opacity:0"/>
        <circle id="tcDotSpy" r="5" fill="{CHART_COLOR_SPY}" class="hover-dot" style="opacity:0"/>
        <rect x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{plot_h}" fill="transparent" id="tcHitRect" style="cursor:crosshair"/>
      </svg>
      <div class="chart-tooltip" id="tcTooltip" style="opacity:0"></div>
    </div>
    <div class="legend">
      <span><i style="background:{CHART_COLOR_ACCOUNT}"></i>本系统累计收益</span>
      <span><i style="background:{CHART_COLOR_SPY}"></i>SPY同期累计</span>
      {f"<span><i style='background:var(--muted)'></i>{esc(marker_label or marker_date)}(分割线)前为旧策略,已停用</span>" if marker_svg else ""}
    </div>
    <script>
    (function() {{
      var data = {point_data};
      var svg = document.getElementById('trendChartSvg');
      var wrap = document.getElementById('trendChartWrap');
      var hit = document.getElementById('tcHitRect');
      var crosshair = document.getElementById('tcCrosshair');
      var dotAcc = document.getElementById('tcDotAcc');
      var dotSpy = document.getElementById('tcDotSpy');
      var tooltip = document.getElementById('tcTooltip');
      if (!svg || !hit || data.length < 2) return;
      var PAD_L = {PAD_L}, PLOT_W = {plot_w}, N = data.length;
      function xAt(i) {{ return PAD_L + (i / (N - 1)) * PLOT_W; }}
      function yAt(v) {{ return {PAD_T} + (1 - (v - ({v_min})) / ({v_span})) * {plot_h}; }}
      function fmtUsd(n) {{
        if (n === null || n === undefined) return 'n/a';
        return (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
      }}
      function show(evt) {{
        var pt = svg.createSVGPoint();
        var clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
        pt.x = clientX; pt.y = 0;
        var svgP = pt.matrixTransform(svg.getScreenCTM().inverse());
        var frac = (svgP.x - PAD_L) / PLOT_W;
        var idx = Math.round(frac * (N - 1));
        if (idx < 0) idx = 0;
        if (idx > N - 1) idx = N - 1;
        var d = data[idx];
        var x = xAt(idx);
        crosshair.setAttribute('x1', x); crosshair.setAttribute('x2', x);
        crosshair.style.opacity = 1;
        dotAcc.setAttribute('cx', x); dotAcc.setAttribute('cy', yAt(d.a)); dotAcc.style.opacity = 1;
        dotSpy.setAttribute('cx', x); dotSpy.setAttribute('cy', yAt(d.s)); dotSpy.style.opacity = 1;
        var chgSign = (d.chg === null || d.chg === undefined) ? '' : (d.chg >= 0 ? '+' : '');
        tooltip.innerHTML =
          '<div class="tt-date"></div>' +
          '<div class="tt-row"><span class="tt-key" style="background:{CHART_COLOR_ACCOUNT}"></span>本系统 <b></b></div>' +
          '<div class="tt-row tt-usd"><span class="tt-key" style="visibility:hidden"></span>总资产 <b></b> <span class="tt-chg"></span></div>' +
          '<div class="tt-row"><span class="tt-key" style="background:{CHART_COLOR_SPY}"></span>SPY <b></b></div>';
        tooltip.querySelector('.tt-date').textContent = d.d;
        tooltip.querySelectorAll('.tt-row b')[0].textContent = (d.a >= 0 ? '+' : '') + d.a.toFixed(2) + '%';
        tooltip.querySelectorAll('.tt-row b')[1].textContent = fmtUsd(d.eq);
        tooltip.querySelector('.tt-chg').textContent = (d.eq === null || d.eq === undefined) ? '' :
          ('(' + chgSign + fmtUsd(d.chg) + ')');
        tooltip.querySelectorAll('.tt-row b')[2].textContent = (d.s >= 0 ? '+' : '') + d.s.toFixed(2) + '%';
        tooltip.style.opacity = 1;
        var wrapRect = wrap.getBoundingClientRect();
        var svgRect = svg.getBoundingClientRect();
        var pxX = svgRect.left - wrapRect.left + (x / {W}) * svgRect.width;
        var left = pxX + 14;
        if (left + 170 > wrapRect.width) left = pxX - 184;
        tooltip.style.left = Math.max(4, left) + 'px';
        tooltip.style.top = '8px';
      }}
      function hide() {{
        crosshair.style.opacity = 0; dotAcc.style.opacity = 0; dotSpy.style.opacity = 0;
        tooltip.style.opacity = 0;
      }}
      hit.addEventListener('pointermove', show);
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('pointerleave', hide);
      hit.addEventListener('touchmove', function(e) {{ show(e); e.preventDefault(); }}, {{passive: false}});
    }})();
    </script>
    """


def render_html(sub_pct, spy_pct, all_time_pct, pos_rows, state, history, watchback, lessons, full_track, equity):
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
            f"<tr><td class='sym'>{sym_label(sym)}</td><td>{w*100:.1f}%</td>"
            f"<td class='status {'ok' if entered else 'pending'}'>{status}</td>"
            f"<td class='reason'>{esc(reason)}</td></tr>\n"
        )

    pos_table_rows = ""
    for r in pos_rows:
        cls = 'pos' if r['plpc'] >= 0 else 'neg'
        pos_table_rows += (
            f"<tr><td class='sym'>{sym_label(r['symbol'])}</td><td>{r['weight_pct']:.1f}%</td>"
            f"<td class='{cls}'>{r['plpc']:+.2f}%</td><td class='{cls}'>{r['day_plpc']:+.2f}%</td></tr>\n"
        )
    if not pos_table_rows:
        pos_table_rows = "<tr><td colspan='4' class='muted'>当前空仓(资金在现金/美债)</td></tr>"

    history_rows = ""
    for h in reversed(history):
        picks_str = ", ".join(f"{sym_label(s)}({w*100:.0f}%)" for s, w in h.get('weights', {}).items()) or "空仓"
        d_pl = h.get('final_pl_pct')
        spy_p = h.get('spy_pct')
        cls = 'pos' if (d_pl or 0) >= 0 else 'neg'
        d_pl_str = f"{d_pl:+.2f}%" if d_pl is not None else "n/a"
        spy_p_str = f"{spy_p:+.2f}%" if spy_p is not None else "n/a"
        history_rows += (
            f"<tr><td>{esc(h.get('date'))}</td><td class='{cls}'>{d_pl_str}</td>"
            f"<td>{spy_p_str}</td><td class='reason'>{picks_str}</td></tr>\n"
        )

    action_log = state.get('action_log', [])[-25:]
    log_items = "".join(f"<li>{esc(a)}</li>\n" for a in reversed(action_log)) or "<li class='muted'>今天暂无操作记录</li>"

    wb_rows = ""
    for e in watchback:
        wb_rows += f"<li><span class='sym'>{sym_label(e.get('symbol'))}</span> {esc(e.get('date'))}卖出 @ ${e.get('exit_price')} — {esc(e.get('reason', ''))[:200]}</li>\n"
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
  .zh {{ color: var(--muted); font-weight: 400; font-size: 12px; }}
  .trend-chart {{ width: 100%; height: auto; display: block; }}
  .trend-chart-wrap {{ position: relative; }}
  .axis-lab {{ font-size: 11px; fill: var(--muted); }}
  .zero-line {{ stroke: var(--border); stroke-width: 1; stroke-dasharray: 3 3; }}
  .regime-line {{ stroke: var(--pending); stroke-width: 1; stroke-dasharray: 4 3; }}
  .regime-lab {{ font-size: 11px; fill: var(--pending); }}
  .end-label {{ font-size: 12px; font-weight: 600; }}
  .crosshair-line {{ stroke: var(--muted); stroke-width: 1; pointer-events: none; }}
  .hover-dot {{ stroke: var(--panel); stroke-width: 2; pointer-events: none; }}
  .chart-tooltip {{
    position: absolute; pointer-events: none; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px;
    font-size: 12px; line-height: 1.6; white-space: nowrap; z-index: 5;
    box-shadow: 0 4px 16px rgba(0,0,0,.35); transition: opacity .05s linear;
  }}
  .tt-date {{ color: var(--muted); margin-bottom: 4px; font-size: 11px; }}
  .tt-row {{ display: flex; align-items: center; gap: 6px; }}
  .tt-row b {{ margin-left: auto; padding-left: 12px; }}
  .tt-usd b {{ color: var(--text); }}
  .tt-chg {{ color: var(--muted); font-size: 11px; }}
  .tt-key {{ display: inline-block; width: 8px; height: 2px; border-radius: 1px; }}
  .legend {{ display: flex; gap: 20px; margin-top: 8px; font-size: 12px; color: var(--muted); }}
  .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }}
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
  <div class="sub" style="margin-top:-20px;">
    页面日期均为美股交易日(UTC/美东时间),与你本地日历可能相差半天到一天(时区差异,不是数据滞后)
  </div>

  <div class="cards">
    <div class="card"><div class="label">账户总资产</div><div class="value">${equity:,.2f}</div></div>
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
    <h2>累计收益走势(完整历史,共 {len(full_track) or len(history)} 个交易日,已扣除出入金影响)</h2>
    <div class="muted" style="font-size:12px;margin-bottom:6px;">
      黄色虚线 = 当前每日自动交易系统上线日({INCEPTION_DATE});此前为已停用的旧策略/实验阶段,
      两段不能按同一条策略评价——"自inception以来"卡片只统计虚线之后。
    </div>
    {render_trend_chart(full_track or history, marker_date=INCEPTION_DATE, marker_label="当前策略上线") or "<p class='muted'>数据积累中,还不足以画出走势图</p>"}
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


WRANGLER_BIN = '/data/qbao775/miniconda3/bin/wrangler'


def deploy():
    # 2026-08-12: 'wrangler' (bare name, PATH lookup) worked fine when tested
    # interactively but silently failed every single cron tick since the day
    # after this was built -- cron's PATH doesn't include
    # /data/qbao775/miniconda3/bin, so subprocess.run raised FileNotFoundError,
    # main() crashed inside deploy(), and the site sat on the same stale
    # deployment for a full day while positions/P&L kept changing underneath
    # it. User noticed via "你昨天做的网站上没有实时更新收益和持仓吗". Use the
    # absolute path so this doesn't depend on whatever PATH the caller has.
    env = dict(os.environ)
    # wrangler's shebang execs `node` via PATH lookup -- cron's PATH
    # (/sbin:/bin:/usr/sbin:/usr/bin) has neither that nor wrangler itself,
    # so the absolute WRANGLER_BIN path alone isn't enough on its own.
    env['PATH'] = '/data/qbao775/miniconda3/bin:' + env.get('PATH', '')
    if os.path.exists(CF_ENV_FILE):
        for line in open(CF_ENV_FILE).read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    r = subprocess.run(
        [WRANGLER_BIN, 'pages', 'deploy', BUILD_DIR, '--project-name', PROJECT_NAME,
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
        sub_pct, spy_pct, all_time_pct, pos_rows, equity = fetch_live_data()
    except Exception as e:
        log(f"failed to fetch live data: {e}")
        return
    state = load_state()
    history = load_history()
    watchback = load_watchback()
    lessons = load_lessons()
    try:
        full_track = fetch_full_track_record()
    except Exception as e:
        log(f"failed to fetch full track record, falling back to short history: {e}")
        full_track = []

    os.makedirs(BUILD_DIR, exist_ok=True)
    out_path = os.path.join(BUILD_DIR, 'index.html')
    open(out_path, 'w').write(render_html(sub_pct, spy_pct, all_time_pct, pos_rows, state, history, watchback, lessons, full_track, equity))
    log(f"rendered dashboard (sub={sub_pct:+.2f}% spy={spy_pct:+.2f}% positions={len(pos_rows)})")
    deploy()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
