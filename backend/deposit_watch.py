#!/usr/bin/env python3
"""deposit_watch.py — detect a new cash deposit and let the engine invest it.

User (2026-06-23) wired ~3000 NZD (~$1.8k USD) and wants it auto-detected and
invested. Runs from cron every 30 min: compares Alpaca's total net deposits to a
stored baseline; on a NEW deposit it emails the user and ensures auto_trade is ON
so the engine deploys the fresh cash into Serenity names within the guardrails
(40% regime cash floor, 12%/name cap, 10-day min-hold, NO margin — multiplier=1).

Idempotent: updates the baseline so each deposit alerts once.
"""
import sys, os, smtplib, datetime, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import SessionLocal, get_setting, set_setting
from email.mime.text import MIMEText

A = "https://api.alpaca.markets"


def log(m): print(f"{datetime.datetime.utcnow().isoformat()}Z  {m}", flush=True)


def email(db, subject, body):
    try:
        s = get_setting(db, "email_sender", 1, ""); pw = get_setting(db, "email_app_password", 1, "")
        r = get_setting(db, "email_recipient", 1, "")
        if not (s and pw and r): return
        msg = MIMEText(body, "plain", "utf-8"); msg["Subject"] = subject
        msg["From"] = s; msg["To"] = r
        srv = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        srv.login(s, pw); srv.send_message(msg); srv.quit()
        log(f"emailed: {subject}")
    except Exception as e:
        log(f"email failed: {e}")


def main():
    db = SessionLocal()
    H = {"APCA-API-KEY-ID": get_setting(db, "alpaca_api_key", 1, ""),
         "APCA-API-SECRET-KEY": get_setting(db, "alpaca_secret_key", 1, "")}
    acts = requests.get(f"{A}/v2/account/activities?activity_types=CSD,JNLC&page_size=100",
                        headers=H, timeout=15).json()
    total = round(sum(float(a.get("net_amount", 0)) for a in acts), 2)

    baseline = get_setting(db, "deposits_seen_total", 1, "")
    if baseline == "":                      # first run → record, don't alert
        set_setting(db, "deposits_seen_total", str(total), 1)
        log(f"baseline set to ${total:.0f} (no alert on first run)")
        return

    base = float(baseline)
    if total <= base + 1:
        log(f"no new deposit (total ${total:.0f} == baseline ${base:.0f})")
        return

    new_amt = total - base
    acct = requests.get(f"{A}/v2/account", headers=H, timeout=15).json()
    cash = float(acct["cash"]); eq = float(acct["equity"])
    set_setting(db, "deposits_seen_total", str(total), 1)
    # ensure the engine will deploy it (margin already impossible: multiplier=1)
    set_setting(db, "auto_trade_enabled", "true", 1)
    regime = get_setting(db, "market_regime", 1, "?"); floor = get_setting(db, "cash_reserve_pct", 1, "40")
    log(f"💰 NEW DEPOSIT ${new_amt:.0f} detected → cash ${cash:.0f}, equity ${eq:.0f}. auto_trade ON.")
    email(db, f"💰 入金到账 ${new_amt:.0f} — SerenityAlphaTrader 将自动部署",
          f"检测到新入金 ${new_amt:.0f}。\n\n"
          f"当前现金 ${cash:.0f} / 净值 ${eq:.0f}。\n"
          f"市场体制 {regime}(现金保底 {floor}%),引擎已开启自动交易,将按 Serenity 卡点策略"
          f"把可部署现金投入(每只≤12%、最短持有10天、-5%止损、无杠杆 multiplier=1)。\n\n"
          f"具体成交见今晚的每日报告。")


if __name__ == "__main__":
    main()
