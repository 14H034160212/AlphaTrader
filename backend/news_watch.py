#!/usr/bin/env python3
"""news_watch.py — proactive breaking-news monitor for our holdings + themes.

Gap found 2026-06-23 (user: "did we catch the Korea chip crash?"): the system
pulls news on-demand but never proactively ALERTS on material market events.
This Exa-searches our current holdings + key thesis sectors (memory/HBM, CPO/
optics, Korea, Fed/rates), flags items carrying material-risk keywords, and
emails the user a short alert. Decision-support only — it never trades; the
engine's -5% stop + regime exposure handle actual risk. Cron a few times/day.
"""
import sys, os, re, smtplib, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import SessionLocal, get_setting, set_setting
from email.mime.text import MIMEText
import requests

A = "https://api.alpaca.markets"
MCPORTER = "/data/qbao775/miniconda3/bin/mcporter"
RISK_KW = ("plunge", "crash", "selloff", "sell-off", "tumble", "slump", "rout",
           "downgrade", "cut", "warning", "warn", "miss", "glut", "oversupply",
           "circuit breaker", "halt", "slash", "weak", "disappoint", "probe")


def log(m): print(f"{datetime.datetime.utcnow().isoformat()}Z  {m}", flush=True)


def exa(query, n=4):
    try:
        out = subprocess.run([MCPORTER, "call", "exa.web_search_exa",
                              f"query={query}", f"numResults={n}"],
                             capture_output=True, text=True, timeout=90,
                             cwd="/data/qbao775/AlphaTrader").stdout
        return out
    except Exception as e:
        log(f"exa fail: {e}"); return ""


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
    pos = requests.get(f"{A}/v2/positions", headers=H, timeout=15).json()
    held = sorted({p["symbol"] for p in pos if float(p.get("qty", 0)) > 0.001})
    held_q = " ".join(held)

    queries = [
        (f"{held_q} stock news today", "我们的持仓"),
        ("semiconductor memory HBM DRAM SK Hynix Samsung Micron selloff news today", "内存/半导体板块"),
        ("AI optics CPO co-packaged optics Coherent Lumentum AAOI news today", "CPO/光通信板块"),
    ]
    # pull headlines, keep only the ones carrying a material-risk keyword
    alerts = []
    for q, label in queries:
        raw = exa(q, 4)
        for line in raw.splitlines():
            m = re.match(r"\s*Title:\s*(.+)", line)
            if not m:
                continue
            title = m.group(1).strip()
            if any(k in title.lower() for k in RISK_KW):
                alerts.append(f"[{label}] {title[:150]}")

    # de-dup, and only alert on items not seen before (stored signature)
    alerts = list(dict.fromkeys(alerts))
    seen = set(filter(None, get_setting(db, "news_watch_seen", 1, "").split("||")))
    fresh = [a for a in alerts if a not in seen]
    if not fresh:
        log(f"no fresh material news ({len(alerts)} risk-headlines, all seen)")
        return

    set_setting(db, "news_watch_seen", "||".join((list(seen) + fresh)[-60:]), 1)
    body = ("检测到与你持仓/板块相关的重大新闻(仅提醒,引擎不会因此自动交易;"
            "-5%止损 + 体制敞口在兜底):\n\n  • " + "\n  • ".join(fresh) +
            f"\n\n当前持仓: {held_q}\n如需我评估是否调整,回我一句即可。")
    email(db, f"⚠️ 持仓相关重大新闻 {len(fresh)} 条 — SerenityAlphaTrader", body)
    log(f"ALERTED {len(fresh)} fresh items")


if __name__ == "__main__":
    main()
