#!/bin/bash
# Daily 3-market reports + broker capability change notification.
# Cron'd nightly. Sends 3 separate emails (US / HK / CN).
# Capability check: notifies on Moomoo Stock Connect (CN) or other markets
# getting enabled, so user knows the moment access opens.
#
# Schedule via crontab: 0 21 * * * /data/qbao775/AlphaTrader/send_market_reports.sh
# (21:00 UTC = NZT 09:00 next-day = after US close)

LOG=/tmp/market_reports.log
PY=/data/qbao775/miniconda3/envs/alphatrader/bin/python
STATE_FILE=/data/qbao775/AlphaTrader/.broker_capabilities.json

echo "============================================" >> "$LOG"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Market reports START" >> "$LOG"

cd /data/qbao775/AlphaTrader/backend
$PY <<'EOF' >> "$LOG" 2>&1
import os, sys, json, smtplib, datetime
sys.path.insert(0, ".")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from database import SessionLocal, Trade, AISignal, Settings, get_setting
from sqlalchemy import desc, func
import requests, futu as ft

db = SessionLocal()
now = datetime.datetime.utcnow()
yesterday_start = now - datetime.timedelta(hours=24)
sender = get_setting(db,"email_sender",1,"")
pw = get_setting(db,"email_app_password",1,"")
recip = get_setting(db,"email_recipient",1,"")

STATE_FILE = "/data/qbao775/AlphaTrader/.broker_capabilities.json"


def send(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender; msg["To"] = recip
    msg.attach(MIMEText(html, "html"))
    try:
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw); s.send_message(msg); s.quit()
        print(f"  ✓ sent: {subject}")
        return True
    except Exception as e:
        print(f"  ✗ send fail: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────
# 1. CAPABILITY CHECK — detect newly-enabled markets
# ──────────────────────────────────────────────────────────────────────────
def snapshot_capabilities():
    """Returns dict of market → True/False / value flags for capability monitoring."""
    caps = {}
    # Moomoo per-market account presence
    for mkt_name, mkt_enum in [("HK",ft.TrdMarket.HK),("US",ft.TrdMarket.US),
                                ("CN",ft.TrdMarket.CN),("JP",ft.TrdMarket.JP),
                                ("SG",ft.TrdMarket.SG),("AU",ft.TrdMarket.AU)]:
        try:
            ctx = ft.OpenSecTradeContext(filter_trdmarket=mkt_enum, host="127.0.0.1", port=11111,
                                          security_firm=ft.SecurityFirm.FUTUAU)
            ret, data = ctx.get_acc_list()
            ctx.close()
            has_real = (ret == ft.RET_OK and len(data) > 0 and
                        any(data['trd_env']=='REAL'))
            caps[f"moomoo_{mkt_name}"] = has_real
        except Exception:
            caps[f"moomoo_{mkt_name}"] = None
    caps["ibkr_enabled_in_db"] = get_setting(db,"ibkr_enabled",1,"false") == "true"

    # IBKR account state (funded? account-ready? market permissions?)
    try:
        from ib_insync import IB, Stock
        ib = IB()
        ib.connect("127.0.0.1", 4001, clientId=99, timeout=10)
        acc = "U23993255"
        # Buying power + funded status
        s_map = {s.tag: s for s in ib.accountSummary(acc)}
        nl = float(s_map.get("NetLiquidation").value) if s_map.get("NetLiquidation") else 0.0
        caps["ibkr_net_liquidation"] = nl
        caps["ibkr_funded"] = nl > 1.0
        bp = float(s_map.get("BuyingPower").value) if s_map.get("BuyingPower") else 0.0
        caps["ibkr_buying_power"] = bp
        # AccountReady flag
        vals = ib.accountValues(acc)
        ready = any(v.tag == "AccountReady" and v.value.lower() == "true" for v in vals)
        caps["ibkr_account_ready"] = ready
        # Market permissions via contract resolution
        market_probes = [
            ("ibkr_perm_UK", "HSBA", "LSE", "GBP"),
            ("ibkr_perm_DE", "SAP",  "XETRA", "EUR"),
            ("ibkr_perm_JP", "7203", "TSE", "JPY"),
            ("ibkr_perm_KR", "005930","KSE","KRW"),
            ("ibkr_perm_NZ", "AIA",  "NZX", "NZD"),
        ]
        for k, t, ex, cy in market_probes:
            try:
                d = ib.reqContractDetails(Stock(t, ex, cy))
                caps[k] = bool(d)
            except Exception:
                caps[k] = False
        ib.disconnect()
    except Exception as e:
        print(f"  IBKR probe failed: {e}")
        caps["ibkr_probe_error"] = str(e)[:120]
    return caps


def load_previous_caps():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_caps(caps):
    with open(STATE_FILE, "w") as f:
        json.dump(caps, f, indent=2)


cur_caps = snapshot_capabilities()
prev_caps = load_previous_caps()
newly_enabled = []
for k, v in cur_caps.items():
    if v and not prev_caps.get(k):
        newly_enabled.append(k)
save_caps(cur_caps)
print(f"Capability state: {cur_caps}")
print(f"Newly enabled: {newly_enabled}")


# ──────────────────────────────────────────────────────────────────────────
# 2. US REPORT (Alpaca)
# ──────────────────────────────────────────────────────────────────────────
def build_us_report():
    H = {"APCA-API-KEY-ID":get_setting(db,"alpaca_api_key",1,""),
         "APCA-API-SECRET-KEY":get_setting(db,"alpaca_secret_key",1,"")}
    acc = requests.get("https://api.alpaca.markets/v2/account", headers=H, timeout=10).json()
    pos = requests.get("https://api.alpaca.markets/v2/positions", headers=H, timeout=10).json()
    orders = requests.get("https://api.alpaca.markets/v2/orders?status=all&after="+
                          (now - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")+
                          "&limit=50", headers=H, timeout=10).json()
    fills = [o for o in orders if o.get('filled_at')]

    equity = float(acc.get('equity',0)); day_pnl = equity - float(acc.get('last_equity',0))
    pnl_color = "#28a745" if day_pnl >= 0 else "#dc3545"
    unrealized = sum(float(p.get('unrealized_pl',0)) for p in pos if float(p.get('qty',0))>0.001)

    rows = ""
    for p in sorted([p for p in pos if float(p['qty'])>0.001], key=lambda x: -float(x.get('market_value',0))):
        mv = float(p.get('market_value',0)); pnl = float(p.get('unrealized_pl',0))
        pct = float(p.get('unrealized_plpc',0))*100
        c = "#28a745" if pnl>=0 else "#dc3545"
        rows += f"<tr><td>{p['symbol']}</td><td>{p['qty']}</td><td>${p['avg_entry_price']}</td><td>${p['current_price']}</td><td>${mv:.2f}</td><td style='color:{c}'>${pnl:+.2f} ({pct:+.1f}%)</td></tr>"

    trade_rows = ""
    for o in sorted(fills, key=lambda x: x['filled_at']):
        side = o['side'].upper()
        c = "#28a745" if side=="BUY" else "#dc3545"
        trade_rows += f"<tr><td>{o['filled_at'][:19]}</td><td style='color:{c}'><b>{side}</b></td><td>{o['symbol']}</td><td>{o['filled_qty']}</td><td>${o['filled_avg_price']}</td></tr>"

    html = f"""<html><body style='font-family:sans-serif; max-width:800px; margin:auto'>
    <h2>🇺🇸 美股报告 — {now.strftime('%Y-%m-%d')}</h2>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%'>
    <tr><td>Equity</td><td><b>${equity:.2f}</b></td>
        <td>今日 P&L</td><td style='color:{pnl_color}'><b>${day_pnl:+.2f}</b></td>
        <td>未实现 P&L</td><td><b>${unrealized:+.2f}</b></td></tr>
    <tr><td>Cash</td><td>${acc.get('cash')}</td>
        <td>Buying Power</td><td>${acc.get('buying_power')}</td>
        <td>Status</td><td>{acc.get('status')}</td></tr>
    </table>
    <h3>当前持仓 ({len([p for p in pos if float(p['qty'])>0.001])})</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:13px'>
    <tr style='background:#f0f0f0'><th>标的</th><th>数量</th><th>入价</th><th>现价</th><th>市值</th><th>未实现 P&L</th></tr>
    {rows}
    </table>
    <h3>24h 内成交 ({len(fills)})</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:12px'>
    <tr style='background:#f0f0f0'><th>时间 UTC</th><th>操作</th><th>标的</th><th>数量</th><th>价格</th></tr>
    {trade_rows or "<tr><td colspan=5>无成交</td></tr>"}
    </table>
    <p style='color:#888; font-size:11px'>SerenityAlphaTrader US-only daily report · {now.isoformat()} UTC</p>
    </body></html>"""
    return html, equity, day_pnl


# ──────────────────────────────────────────────────────────────────────────
# 3. HK REPORT (Moomoo HK)
# ──────────────────────────────────────────────────────────────────────────
def build_hk_report():
    info_str = ""; positions_html = "<tr><td colspan=6>无持仓</td></tr>"
    trades_html = "<tr><td colspan=5>无成交</td></tr>"
    try:
        ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK, host="127.0.0.1", port=11111,
                                       security_firm=ft.SecurityFirm.FUTUAU)
        ret, info = ctx.accinfo_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True, currency=ft.Currency.HKD)
        if ret == ft.RET_OK:
            row = info.iloc[0]
            info_str = (f"HKD Cash: HK${row.get('cash',0):.2f} · "
                        f"Total Assets: HK${row.get('total_assets',0):.2f} · "
                        f"Buying Power: {row.get('power','N/A')}")
        # Positions
        ret2, ps = ctx.position_list_query(trd_env=ft.TrdEnv.REAL)
        if ret2 == ft.RET_OK and len(ps) > 0:
            rows = ""
            for _, p in ps.iterrows():
                rows += f"<tr><td>{p.get('code')}</td><td>{p.get('stock_name','')}</td><td>{p.get('qty',0)}</td><td>{p.get('cost_price','?')}</td><td>{p.get('market_val',0)}</td><td>{p.get('pl_val',0)}</td></tr>"
            positions_html = rows
        # Today's filled orders
        ret3, ords = ctx.order_list_query(trd_env=ft.TrdEnv.REAL)
        if ret3 == ft.RET_OK and len(ords) > 0:
            filled = ords[ords['order_status']=='FILLED_ALL'] if 'order_status' in ords.columns else ords
            if len(filled) > 0:
                trows = ""
                for _, o in filled.iterrows():
                    trows += f"<tr><td>{o.get('create_time','')[:19]}</td><td>{o.get('trd_side')}</td><td>{o.get('code')}</td><td>{o.get('qty')}</td><td>{o.get('price')}</td></tr>"
                trades_html = trows
        ctx.close()
    except Exception as e:
        info_str = f"Moomoo HK 查询失败: {e}"

    # AI signals on HK names in last 24h
    sigs = (db.query(AISignal).filter(AISignal.timestamp >= yesterday_start,
                                       AISignal.symbol.like('%.HK'))
                              .order_by(desc(AISignal.timestamp)).limit(15).all())
    sigs_html = ""
    for s in sigs:
        sigs_html += f"<tr><td>{s.timestamp.strftime('%H:%M')}</td><td>{s.symbol}</td><td>{s.signal}</td><td>{s.confidence}</td></tr>"

    html = f"""<html><body style='font-family:sans-serif; max-width:800px; margin:auto'>
    <h2>🇭🇰 港股报告 — {now.strftime('%Y-%m-%d')}</h2>
    <p><b>{info_str}</b></p>
    <h3>当前 HK 持仓</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:13px'>
    <tr style='background:#f0f0f0'><th>代码</th><th>名称</th><th>数量</th><th>入价</th><th>市值</th><th>盈亏</th></tr>
    {positions_html}
    </table>
    <h3>24h 内 HK 成交</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:12px'>
    <tr style='background:#f0f0f0'><th>时间</th><th>操作</th><th>代码</th><th>数量</th><th>价格</th></tr>
    {trades_html}
    </table>
    <h3>24h 内 HK AI 信号 ({len(sigs)})</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:12px'>
    <tr style='background:#f0f0f0'><th>时间</th><th>标的</th><th>信号</th><th>置信</th></tr>
    {sigs_html or "<tr><td colspan=4>无信号</td></tr>"}
    </table>
    <p style='color:#888; font-size:11px'>SerenityAlphaTrader HK-only daily report</p>
    </body></html>"""
    return html


# ──────────────────────────────────────────────────────────────────────────
# 4. CN REPORT (3-layer: ADR + HK-listed China + A-share)
# ──────────────────────────────────────────────────────────────────────────
def build_cn_report():
    # Layer 1: China ADR positions in Alpaca
    H = {"APCA-API-KEY-ID":get_setting(db,"alpaca_api_key",1,""),
         "APCA-API-SECRET-KEY":get_setting(db,"alpaca_secret_key",1,"")}
    china_adr_tkrs = {"BABA","BIDU","JD","PDD","NTES","TCOM","NIO","LI","XPEV","BILI"}
    pos_us = requests.get("https://api.alpaca.markets/v2/positions", headers=H, timeout=10).json()
    adr_rows = ""
    adr_count = 0
    for p in pos_us:
        if p['symbol'] in china_adr_tkrs and float(p.get('qty',0))>0.001:
            adr_count += 1
            mv = float(p.get('market_value',0)); pnl = float(p.get('unrealized_pl',0))
            adr_rows += f"<tr><td>{p['symbol']}</td><td>ADR (Alpaca)</td><td>{p['qty']}</td><td>${p['current_price']}</td><td>${mv:.2f}</td><td>${pnl:+.2f}</td></tr>"

    # Layer 2: HK-listed China + A-share via Moomoo HK
    hk_china_rows = ""
    hk_count = 0
    try:
        ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.HK, host="127.0.0.1", port=11111,
                                       security_firm=ft.SecurityFirm.FUTUAU)
        ret, ps = ctx.position_list_query(trd_env=ft.TrdEnv.REAL)
        if ret == ft.RET_OK and len(ps) > 0:
            for _, p in ps.iterrows():
                hk_count += 1
                hk_china_rows += f"<tr><td>{p.get('code')}</td><td>HK 港股</td><td>{p.get('qty')}</td><td>{p.get('nominal_price','?')}</td><td>{p.get('market_val',0)}</td><td>{p.get('pl_val',0)}</td></tr>"
        ctx.close()
    except Exception:
        pass

    # Capability status
    cn_status = "✅ 已开通" if cur_caps.get("moomoo_CN") else "❌ <b>未开通</b> — 请在 Moomoo App 内申请 Stock Connect 权限"

    # Catalysts on China names in 24h
    china_syms = list(china_adr_tkrs) + ["0700.HK","9988.HK","3690.HK","1810.HK","9618.HK","BYD","1024.HK"]
    sigs = (db.query(AISignal).filter(AISignal.timestamp >= yesterday_start,
                                       AISignal.symbol.in_(china_syms),
                                       AISignal.confidence >= 0.7)
                              .order_by(desc(AISignal.timestamp)).limit(15).all())
    sigs_html = ""
    for s in sigs:
        sigs_html += f"<tr><td>{s.timestamp.strftime('%H:%M')}</td><td>{s.symbol}</td><td>{s.signal}</td><td>{s.confidence}</td></tr>"

    html = f"""<html><body style='font-family:sans-serif; max-width:800px; margin:auto'>
    <h2>🇨🇳 中国市场报告 — {now.strftime('%Y-%m-%d')}</h2>

    <h3>3 层访问状态</h3>
    <ul>
    <li>Layer 1: 中概 ADR (BABA/BIDU/JD/PDD…) → <b style='color:green'>✅ Alpaca 可 trade</b></li>
    <li>Layer 2: HK 上市中国 (0700/9988/3690…) → <b style='color:orange'>⚠️ Moomoo HK 代码就绪，等 buying power 解锁</b></li>
    <li>Layer 3: A股 (Stock Connect) → {cn_status}</li>
    </ul>

    <h3>中国持仓汇总 (ADR {adr_count} + HK 港股 {hk_count})</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:13px'>
    <tr style='background:#f0f0f0'><th>标的</th><th>层</th><th>数量</th><th>现价</th><th>市值</th><th>盈亏</th></tr>
    {adr_rows}{hk_china_rows}
    {("<tr><td colspan=6>无中国敞口</td></tr>" if adr_count+hk_count==0 else "")}
    </table>

    <h3>24h 中国相关 AI 信号 (conf ≥ 0.70)</h3>
    <table border=1 cellpadding=5 style='border-collapse:collapse; width:100%; font-size:12px'>
    <tr style='background:#f0f0f0'><th>时间</th><th>标的</th><th>信号</th><th>置信</th></tr>
    {sigs_html or "<tr><td colspan=4>无信号</td></tr>"}
    </table>
    <p style='color:#888; font-size:11px'>SerenityAlphaTrader China-only daily report</p>
    </body></html>"""
    return html


# ──────────────────────────────────────────────────────────────────────────
# 5. NEW MARKET BANNER (if anything was newly enabled)
# ──────────────────────────────────────────────────────────────────────────
if newly_enabled:
    banner_html = (f"<html><body style='font-family:sans-serif'>"
                   f"<h2 style='color:#28a745'>🎉 新市场访问已开通！</h2>"
                   f"<p>检测到以下市场的 broker 访问刚刚开通：</p><ul>")
    for m in newly_enabled:
        banner_html += f"<li><b>{m}</b></li>"
    banner_html += ("</ul><p>SerenityAlphaTrader 将在下一轮 background_dynamic_watchlist_loop "
                    "（最长 6h）开始自动扫描新市场标的。</p>"
                    "<p>如果是 Moomoo CN（Stock Connect），auto_trade_loop 也会立刻"
                    "开始路由 A 股订单。</p></body></html>")
    send(f"🎉 [SerenityAlphaTrader] 新市场访问开通: {', '.join(newly_enabled)}", banner_html)


# ──────────────────────────────────────────────────────────────────────────
# 6. SEND ALL 3 REPORTS
# ──────────────────────────────────────────────────────────────────────────
us_html, us_eq, us_pnl = build_us_report()
us_color = "📈" if us_pnl >= 0 else "📉"
send(f"🇺🇸 [SerenityAlphaTrader US] {now.strftime('%m-%d')} {us_color} Equity ${us_eq:.0f}, {us_pnl:+.0f}", us_html)

hk_html = build_hk_report()
send(f"🇭🇰 [SerenityAlphaTrader HK] {now.strftime('%m-%d')} 港股日报", hk_html)

cn_html = build_cn_report()
cn_marker = "✅" if cur_caps.get("moomoo_CN") else "⏳A股待开"
send(f"🇨🇳 [SerenityAlphaTrader CN] {now.strftime('%m-%d')} {cn_marker}", cn_html)

print(f"\n[{datetime.datetime.utcnow().isoformat()}] All 3 market reports sent.")
EOF

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Market reports END" >> "$LOG"
