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
    # include CSW (cash withdrawals) so net deposits drop on a wire-out and the
    # baseline lowers correctly — without CSW a withdrawal never reduces `total`.
    acts = requests.get(f"{A}/v2/account/activities?activity_types=CSD,CSW,JNLC&page_size=100",
                        headers=H, timeout=15)
    acts.raise_for_status()
    data = acts.json()
    total = round(sum(float(a.get("net_amount", 0)) for a in data if isinstance(a, dict)), 2)

    baseline = get_setting(db, "deposits_seen_total", 1, "")
    if baseline == "":                      # first run → record, don't alert
        set_setting(db, "deposits_seen_total", str(total), 1)
        log(f"baseline set to ${total:.0f} (no alert on first run)")
        return

    base = float(baseline)
    if total <= base + 1:
        # Track the baseline DOWN on withdrawals (CSW/negative JNLC) so a later
        # real deposit isn't masked by a stale high baseline.
        if total < base:
            set_setting(db, "deposits_seen_total", str(total), 1)
            log(f"net deposits fell to ${total:.0f} (withdrawal); baseline lowered")
        else:
            log(f"no new deposit (total ${total:.0f} == baseline ${base:.0f})")
        return

    new_amt = total - base
    acct = requests.get(f"{A}/v2/account", headers=H, timeout=15).json()
    cash = float(acct["cash"]); eq = float(acct["equity"])
    set_setting(db, "deposits_seen_total", str(total), 1)
    # 2026-06-30: do NOT auto-enable auto_trade on a deposit. Large deposits are
    # CONSERVATIVE-SLEEVE savings (40/35/25 VOO/BND/SGOV, DCA'd in manually), NOT
    # fuel for the active small-cap strategy. Just alert; deployment is manual.
    log(f"💰 NEW DEPOSIT ${new_amt:.0f} detected → cash ${cash:.0f}, equity ${eq:.0f}. auto_trade NOT touched.")
    email(db, f"💰 入金到账 ${new_amt:.0f} — 等待手动配置(保守组合)",
          f"检测到新入金 ${new_amt:.0f}。\n\n"
          f"当前现金 ${cash:.0f} / 净值 ${eq:.0f}。\n\n"
          f"⚠️ 不会自动炒股。这笔钱按计划进保守 ETF 组合(VOO 40% / BND 35% / SGOV 25%,"
          f"分批 DCA),与主动小盘策略分开管理。我会手动分批部署并向你汇报。")


if __name__ == "__main__":
    main()
