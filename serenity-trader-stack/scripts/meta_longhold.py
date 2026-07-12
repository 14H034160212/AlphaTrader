#!/usr/bin/env python3
"""
meta_longhold.py — long-term META position, user-directed (2026-07-11):
"我觉得可以买meta，而且长期买入" (buy Meta, long-term) followed by
"不要因为波动就卖出，meta现在实力和野心都很大" (don't sell on volatility,
Meta's strength and ambition are strong right now).

Unlike the earlier same-day META trial (bought/sold 2026-07-10 as part of
the day's liquidation, net -$9.97), this is explicitly framed as a real
long-term hold, not a day-trade or momentum chase.

Research basis (checked twice this session, both times positive):
  - Fundamentals (2026-07-10): 18.1x forward PE vs 33% revenue growth,
    32.8% profit margin, price 16% below 52w high, strong_buy across 58
    analysts -- not an expensive, priced-for-perfection entry.
  - Fresh catalysts since (2026-07-11): custom "Iris" AI chip entering
    production September 2026 (designed with Broadcom, built by TSMC,
    stock +6% on the news) directly addresses the market's biggest
    concern about Meta -- whether $125-145B of 2026 AI capex converts to
    efficiency, not just spend. Also launched Muse Spark 1.1 + a public
    Meta Model API, a real strategic move into the paid frontier-model
    business (not just an open-weight/Llama play), competing directly
    with Anthropic/OpenAI/Google.
  - Genuine risk noted: Muse Image privacy backlash (pulled after 3 days)
    + pending EU DMA/state-AG litigation -- regulatory/PR risk, not
    something a stop-loss protects against anyway.

Given this is a considered long-term thesis (not a reversal-of-rejection
like MU, and vetted twice with real data, not just headline reaction),
sized higher than MU: 8% vs MU's 5% -- low end of the "确信/high
conviction" bucket per the account's own sizing framework.

Per explicit instruction, NO stop-loss and NO take-profit target -- this
mirrors MU's setup. Ongoing thesis monitoring is
crossvalidate_satellite.py's job (advisory only, never auto-sells) once
this shows up in get_satellite_positions().
"""
import sys, os, json, datetime
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

_ENV_FILE = '/home/qbao775/serenity-trader-stack/.env'
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

DONE_MARKER = '/home/qbao775/serenity-trader-stack/.meta_longhold_entered'
TARGET_PCT = 0.08   # low end of "high conviction" bucket -- higher than MU's
                    # 5% given this thesis has been vetted twice with real
                    # data, not a reversal-of-rejection


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def get_alpaca():
    from database import SessionLocal, get_setting
    import alpaca_trade_api as tradeapi
    db = SessionLocal()
    k = get_setting(db, 'alpaca_api_key', 1)
    s = get_setting(db, 'alpaca_secret_key', 1)
    u = get_setting(db, 'alpaca_base_url', 1, 'https://api.alpaca.markets')
    db.close()
    return tradeapi.REST(k, s, u)


def send_email(subject, body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from database import SessionLocal, get_setting
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: email_sender/email_app_password/email_recipient not set in DB")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recip
        msg.attach(MIMEText(body, "plain"))
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw)
        s.sendmail(sender, [recip], msg.as_string())
        s.quit()
        log(f"email sent to {recip}")
    except Exception as e:
        log(f"email err: {e}")


def enter_position(api):
    import market_data as md
    acc = api.get_account()
    equity = float(acc.equity)
    bp = float(acc.buying_power)
    target_notional = equity * TARGET_PCT

    q = md.get_stock_quote('META')
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live META price available yet — will retry next tick")
        return

    qty = round(min(target_notional, bp - 20) / px, 4)
    if qty <= 0:
        log(f"  insufficient buying power for META @ ${px} — aborting")
        return

    o = api.submit_order(symbol='META', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  ✓ BOUGHT META qty={qty} @~${px} order={o.id[:8]}")

    with open(DONE_MARKER, 'w') as f:
        json.dump({'entered_at': datetime.datetime.utcnow().isoformat(), 'entry_price_est': px, 'qty': qty}, f)
    send_email("📈 META 长期建仓",
               f"买入 META {qty}股,预估入场价 ~${px}\n"
               f"仓位: {TARGET_PCT*100:.0f}%(长期持有,不设止损、不设止盈目标)\n"
               f"后续由 crossvalidate_satellite.py 的常规4小时论文复核自动跟踪,"
               f"该机制只会提示/升级,不会自动卖出。")


def main():
    if os.path.exists(DONE_MARKER):
        log("META already entered — long-term hold, no stop-loss/take-profit, nothing left "
            "for this script to do. Ongoing monitoring is crossvalidate_satellite.py's job.")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    log("no META position yet — attempting entry")
    enter_position(api)


if __name__ == '__main__':
    main()
