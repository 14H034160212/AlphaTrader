#!/usr/bin/env python3
"""
sell_tel_live_20260729.py -- ONE-OFF, 2026-07-29 ONLY. Sells the live (real
money) TEL position at the market open, mirroring the paper account's AI exit
decision from 2026-07-28 15:33 UTC (sold @ $209.49: "财报利好已一次性兑现,
+6%正是'卖高不追涨'该卖的强势位,落袋为安").

Why this script exists (2026-07-28 evening): the live TEL position was a
one-time manual buy (2026-07-23, 36.3337sh @ $200.66) mirroring the paper
position, but with NO automated management -- when the paper AI exited, the
live side had no mechanism to follow, and Claude only saw the exit hours
later, after close, when after-hours liquidity was too thin to act (bid $192
vs $214 close). User asked "为什么没有和模拟盘一致" -- this closes that gap
for the exit without depending on Claude being invoked at the right moment.

Selling is within standing autonomous authority (sell/trim/monitor:
autonomous, report after the fact). After the sell, proceeds are parked
100% into SGOV per the standing rule ("以后清仓以后就全部买入美债要记住",
"不要留缓冲现金"). One summary email at the end.

Remove the cron entry after 2026-07-29; DONE_MARKER guards re-runs.
"""
import sys, os, json, datetime, time
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

DONE_MARKER = '/home/qbao775/serenity-trader-stack/.sell_tel_live_20260729_done'
SYM = 'TEL'


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def _creds():
    from database import SessionLocal, get_setting
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    return k, s, u


def get_alpaca():
    import alpaca_trade_api as tradeapi
    k, s, u = _creds()
    return tradeapi.REST(k, s, u)


def send_email(subject, body):
    import smtplib
    from email.mime.text import MIMEText
    from database import SessionLocal, get_setting
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: not configured")
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


def main():
    if os.path.exists(DONE_MARKER):
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log("market not open yet -- waiting")
        return

    try:
        p = api.get_position(SYM)
    except Exception:
        log(f"no {SYM} position found -- marking done")
        with open(DONE_MARKER, 'w') as f:
            json.dump({'note': 'no position found', 'at': datetime.datetime.utcnow().isoformat()}, f)
        return

    qty = p.qty
    entry = float(p.avg_entry_price)
    plpc = float(p.unrealized_plpc) * 100
    o = api.submit_order(symbol=SYM, qty=qty, side='sell', type='market', time_in_force='day')
    log(f"✓ SELL {SYM} qty={qty} submitted (entry ${entry}, unrealized {plpc:+.2f}%) order={o.id[:8]}")

    time.sleep(10)
    o2 = api.get_order(o.id)
    fill_px = o2.filled_avg_price
    log(f"sell status={o2.status} filled_avg_price={fill_px}")

    # Park 100% of freed cash into SGOV, no buffer (standing rule)
    import requests
    k, s, _ = _creds()
    acct = api.get_account()
    cash = float(acct.cash)
    sgov_note = "park skipped (cash < $5)"
    if cash >= 5:
        r = requests.get('https://data.alpaca.markets/v2/stocks/SGOV/quotes/latest',
                          headers={'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}, timeout=15)
        ask = r.json()['quote']['ap']
        limit_px = round(ask + 0.05, 2)
        sq = round(cash / limit_px, 4)
        o3 = api.submit_order(symbol='SGOV', qty=sq, side='buy', type='limit',
                               limit_price=limit_px, time_in_force='day')
        sgov_note = f"parked ${cash:.2f} into {sq} SGOV @ limit ${limit_px} order={o3.id[:8]}"
        log(sgov_note)

    with open(DONE_MARKER, 'w') as f:
        json.dump({'sold_qty': str(qty), 'entry': entry, 'fill_px': str(fill_px),
                   'at': datetime.datetime.utcnow().isoformat()}, f)

    realized_pct = (float(fill_px) - entry) / entry * 100 if fill_px else None
    body = (f"实盘 TEL 已按计划在开盘卖出(跟进模拟盘AI 2026-07-28 的离场判断)。\n\n"
            f"数量: {qty} 股\n入价: ${entry}\n成交价: ${fill_px}\n"
            f"已实现收益: {realized_pct:+.2f}%\n\n{sgov_note}\n\n"
            f"背景: 模拟盘AI于07-28 15:33 UTC以$209.49卖出同名仓位(财报利好兑现,卖高不追涨);"
            f"实盘因盘后流动性太差延至今日开盘执行。")
    send_email(f"📊 实盘 TEL 平仓完成 ({datetime.datetime.utcnow():%Y-%m-%d})", body)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION this tick:")
        log(traceback.format_exc())
