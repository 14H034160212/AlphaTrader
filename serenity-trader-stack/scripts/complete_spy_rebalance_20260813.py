#!/usr/bin/env python3
"""
complete_spy_rebalance_20260813.py -- one-time completion step for the
2026-08-13 real-account de-risking the user asked for: "我建议实盘就都买
标普500，模拟盘可以继续验证你的算法" (real account should go all-SPY; the
paper/simulated ledger can keep validating the picking algorithm).

Executed same evening (2026-08-13, market closed): submitted 'day' market
sell orders for CXW and MLTX, which Alpaca accepted and queued (status
'accepted') for the next session open, since the market was already closed
(next_open 2026-08-14 09:30 ET). Could not buy SPY with the proceeds in
that same run because the exact freed cash amount isn't known until the
sells actually fill -- that only happens once the market reopens.

This script runs every 5 minutes during a narrow window right after
tomorrow's open, waits for both sell orders to reach a confirmed 'filled'
status (same fill-verification standard applied earlier today to the 5
long-term scripts -- never trust mere order acceptance), then buys SPY
with all resulting buying power. Writes DONE_MARKER once the SPY buy is
itself confirmed filled, and every subsequent run becomes a no-op --
remove the cron entry once DONE_MARKER exists (self-limiting, not meant
to run forever).
"""
import sys, os, json, datetime
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

DONE_MARKER = '/home/qbao775/serenity-trader-stack/.spy_rebalance_20260813_done'
# Kept inside serenity-trader-stack, NOT the session scratchpad -- the
# scratchpad is session-scoped and isn't guaranteed to survive until
# tomorrow's market open, when this script actually needs to read it.
SELL_ORDERS_FILE = '/home/qbao775/serenity-trader-stack/.spy_rebalance_20260813_sell_orders.json'


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
        log("email skipped: sender/pw/recipient not set in DB")
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


def main():
    if os.path.exists(DONE_MARKER):
        log("rebalance already completed — nothing to do (remove this cron entry)")
        return

    api = get_alpaca()
    clock = api.get_clock()
    if not clock.is_open:
        log(f"market closed (next_open={clock.next_open}) — nothing to do this tick")
        return

    if not os.path.exists(SELL_ORDERS_FILE):
        log("no sell-orders record found — nothing to reconcile, aborting")
        return
    sell_orders = json.load(open(SELL_ORDERS_FILE))

    all_filled = True
    for sym, order_id in sell_orders:
        try:
            o = api.get_order(order_id)
        except Exception as e:
            log(f"  {sym}: couldn't check order {order_id[:8]}: {e}")
            all_filled = False
            continue
        if o.status == 'filled':
            log(f"  {sym}: sell CONFIRMED FILLED qty={o.filled_qty} @~${o.filled_avg_price}")
        elif o.status in ('canceled', 'expired', 'rejected', 'suspended'):
            log(f"  ⚠ {sym}: sell order reached {o.status} without filling — NOT proceeding to SPY "
                f"buy automatically, needs manual attention")
            send_email("⚠️ SPY再平衡中止 — 卖出订单未成交",
                       f"{sym}的卖出订单状态是{o.status}，没有成交。已中止自动买入SPY流程，需要人工检查。")
            return
        else:
            log(f"  {sym}: sell still pending (status={o.status})")
            all_filled = False

    if not all_filled:
        log("not all sell orders confirmed filled yet — will check again next tick")
        return

    # Both sells confirmed filled -- buy SPY with all resulting buying power.
    import market_data as md
    acc = api.get_account()
    bp = float(acc.buying_power)
    q = md.get_stock_quote('SPY')
    px = q['current'] if q and q.get('current') else None
    if not px:
        log("  no live SPY price available yet — will retry next tick")
        return

    qty = round((bp - 20) / px, 4)
    if qty <= 0:
        log(f"  insufficient buying power (${bp:.2f}) to buy SPY @ ${px:.2f} — aborting")
        return

    o = api.submit_order(symbol='SPY', qty=qty, side='buy', type='market', time_in_force='day')
    log(f"  submitted SPY buy qty={qty} order={o.id[:8]}")

    import time as _time
    filled = None
    for _ in range(15):
        _time.sleep(3)
        try:
            check = api.get_order(o.id)
        except Exception:
            continue
        if check.status == 'filled':
            filled = check
            break
        if check.status in ('canceled', 'expired', 'rejected', 'suspended'):
            log(f"  ⚠ SPY buy order reached {check.status} without filling")
            send_email("⚠️ SPY再平衡 — SPY买入未确认成交",
                       f"CXW/MLTX卖出已确认成交，但SPY买入(qty={qty})没能确认成交状态({check.status})，"
                       f"需要人工检查，下一个tick会重试。")
            return

    if not filled:
        log("  ⚠ SPY buy order did not confirm as filled within the wait window -- will re-check next tick "
            "(not marking done, not double-buying)")
        return

    log(f"  ✓ BOUGHT SPY qty={filled.filled_qty} @~${filled.filled_avg_price} order={o.id[:8]}")
    with open(DONE_MARKER, 'w') as f:
        json.dump({'completed_at': datetime.datetime.utcnow().isoformat(),
                    'spy_qty': filled.filled_qty, 'spy_avg_price': filled.filled_avg_price,
                    'sell_orders': sell_orders}, f, indent=2)
    send_email("✅ 实盘再平衡完成 — 已全部转为SPY",
               f"CXW/MLTX已确认卖出成交，买入SPY {filled.filled_qty}股 @~${filled.filled_avg_price}。\n"
               f"实盘现在应该只持有SPY。模拟盘继续按原策略跑，用于验证选股算法，不再镜像到实盘。")
    log("─── 再平衡完成 ───")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log("UNCAUGHT EXCEPTION:")
        log(traceback.format_exc())
