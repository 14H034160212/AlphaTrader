#!/bin/bash
# HK opening check — fires every weekday NZT 13:40 (HK open + 10min)
# Monitors max_cash_buy to detect the moment Moomoo unblocks HK buying power.
# 2026-05-26: schedule changed from Mon-only to Mon-Fri after 1 week of
# max_cash_buy=0. Now polls every trading day to catch the unlock moment.

LOG=/tmp/hk_monday_check.log
PY=/data/qbao775/miniconda3/envs/alphatrader/bin/python

echo "============================================" >> "$LOG"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HK Monday check START" >> "$LOG"

cd /data/qbao775/AlphaTrader/backend
$PY <<'EOF' >> "$LOG" 2>&1
import os, sys, smtplib, json, datetime
sys.path.insert(0, ".")
from database import SessionLocal, Trade, AISignal, get_setting
from sqlalchemy import desc
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

db = SessionLocal()
now_utc = datetime.datetime.utcnow()
today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

# === 1. HK trades today ===
hk_trades = db.query(Trade).filter(
    Trade.market == "HK",
    Trade.timestamp >= today_start - datetime.timedelta(hours=24),
).order_by(desc(Trade.timestamp)).all()
print(f"DB HK trades (last 24h): {len(hk_trades)}")

# === 2. Moomoo broker-side orders ===
import futu as ft
broker_orders = []
broker_err = None
max_buy_02822 = None
hk_cash = None
try:
    ctx = ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.HK,
        host="127.0.0.1", port=11111,
        security_firm=ft.SecurityFirm.FUTUAU,
    )
    ret, orders = ctx.order_list_query(trd_env=ft.TrdEnv.REAL)
    if ret == ft.RET_OK and len(orders) > 0:
        for _, o in orders.iterrows():
            broker_orders.append({
                "code": o.get("code"), "side": o.get("trd_side"),
                "qty": o.get("qty"), "status": o.get("order_status"),
                "create_time": str(o.get("create_time","")),
                "err": (o.get("last_err_msg","") or "")[:120],
            })

    # Check buying power for 02822.HK now
    ret, info = ctx.acctradinginfo_query(
        order_type=ft.OrderType.NORMAL, code="HK.02822",
        price=16.0, trd_env=ft.TrdEnv.REAL,
    )
    if ret == ft.RET_OK:
        max_buy_02822 = float(info.iloc[0].get("max_cash_buy", 0))

    ret, acc = ctx.accinfo_query(trd_env=ft.TrdEnv.REAL, refresh_cache=True, currency=ft.Currency.HKD)
    if ret == ft.RET_OK:
        hk_cash = float(acc.iloc[0].get("cash", 0))
    ctx.close()
except Exception as e:
    broker_err = str(e)

print(f"Moomoo broker orders today: {len(broker_orders)}")
print(f"HK cash: HK${hk_cash}, max_buy 02822.HK: {max_buy_02822}")

# === 3. Determine outcome ===
filled_hk = [t for t in hk_trades if t.broker == "Futu"]
status_emoji = "✅" if filled_hk else ("⚠️" if max_buy_02822 == 0 else "❌")

# Build email
subject = f"[SerenityAlphaTrader] {status_emoji} HK Monday Opening Check — {len(filled_hk)} real fills"

body = [f"<h2>HK Monday Opening Check — {now_utc.isoformat()} UTC</h2>"]
body.append(f"<p><b>Real HK Futu trades (last 24h): {len(filled_hk)}</b></p>")

if filled_hk:
    body.append("<h3>✅ HK fills:</h3><table border=1 cellpadding=5>")
    body.append("<tr><th>Time</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th></tr>")
    for t in filled_hk:
        body.append(f"<tr><td>{t.timestamp}</td><td>{t.side}</td><td>{t.symbol}</td><td>{t.quantity}</td><td>{t.price}</td></tr>")
    body.append("</table>")

body.append(f"<h3>Moomoo broker state</h3>")
body.append(f"<ul>")
body.append(f"<li>HK cash: HK${hk_cash}</li>")
body.append(f"<li>max_cash_buy for 02822.HK: <b>{max_buy_02822}</b>")
if max_buy_02822 == 0:
    body.append(" ❌ <b>broker still blocking — user must contact Moomoo support</b>")
elif max_buy_02822 and max_buy_02822 > 0:
    body.append(f" ✅ <b>buying power restored! Up to {int(max_buy_02822)} shares allowed</b>")
body.append("</li>")
body.append(f"<li>Pending broker orders: {len(broker_orders)}</li>")
body.append("</ul>")

if broker_orders:
    body.append("<h4>Broker orders detail (last 5)</h4><pre>")
    for o in broker_orders[:5]:
        body.append(json.dumps(o, default=str))
    body.append("</pre>")

if broker_err:
    body.append(f"<p style='color:red'>broker query error: {broker_err}</p>")

# Diagnostic actions for tomorrow if still blocked
if not filled_hk and max_buy_02822 == 0:
    body.append("<h3>🛠 Recommended action</h3>")
    body.append("<ol>")
    body.append("<li>Open Moomoo App → try manually buying 200 shares 02822.HK</li>")
    body.append("<li>If app also rejects → contact Moomoo NZ support (+64 800 666 6688) and ask why max_cash_buy=0 when HKD cash=HK$4,629</li>")
    body.append("<li>If app succeeds → file OpenD API bug with Moomoo (App vs API discrepancy)</li>")
    body.append("</ol>")

# ── State-aware email gating (2026-05-27) ──
# Now that this runs HOURLY, only email on a STATE CHANGE so the user isn't
# spammed with "still blocked" every hour. Email only if:
#   (a) there's a real HK fill, OR
#   (b) max_cash_buy transitioned from 0 → positive (the unlock moment), OR
#   (c) HK cash changed materially (deposit landed)
STATE_F = "/tmp/hk_check_state.json"
prev = {}
try:
    with open(STATE_F) as f:
        prev = json.load(f)
except (FileNotFoundError, ValueError):
    prev = {}

prev_max_buy = prev.get("max_buy_02822", 0) or 0
prev_cash = prev.get("hk_cash", 0) or 0
cur_max_buy = max_buy_02822 or 0
cur_cash = hk_cash or 0

unlocked_now = (prev_max_buy == 0 and cur_max_buy > 0)
cash_jumped = abs(cur_cash - prev_cash) > 100   # deposit/withdrawal landed
should_email = bool(filled_hk) or unlocked_now or cash_jumped

# Save current state
with open(STATE_F, "w") as f:
    json.dump({"max_buy_02822": cur_max_buy, "hk_cash": cur_cash,
               "ts": now_utc.isoformat()}, f)

if unlocked_now:
    subject = f"🎉 [SerenityAlphaTrader] HK BUYING POWER UNLOCKED — max_cash_buy={int(cur_max_buy)} shares"
    body.insert(0, "<h1 style='color:#28a745'>🎉 HK trading just unlocked!</h1>"
                   "<p>max_cash_buy went from 0 to positive. SerenityAlphaTrader will place "
                   "its first HK order on the next auto_trade_loop cycle (≤15 min).</p>")
elif cash_jumped:
    subject = f"💰 [SerenityAlphaTrader] HK cash changed: HK${prev_cash:.0f} → HK${cur_cash:.0f}"

if not should_email:
    print(f"  (no state change — max_buy={cur_max_buy}, cash={cur_cash} — skip email)")
else:
    sender = get_setting(db,"email_sender",1,"")
    pw = get_setting(db,"email_app_password",1,"")
    recip = get_setting(db,"email_recipient",1,"")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recip
    msg.attach(MIMEText("\n".join(body), "html"))
    try:
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw)
        s.send_message(msg)
        s.quit()
        print(f"✓ email sent to {recip}: {subject}")
    except Exception as e:
        print(f"✗ email send failed: {e}")
EOF

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HK Monday check END" >> "$LOG"
