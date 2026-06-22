#!/usr/bin/env python3
"""monday_activation.py — guarded auto-trade activator.

User mandate (2026-06-21): "我很忙，请你帮我操作" — operate autonomously per the
adaptive-exposure principle (gain more in up-markets, lose less in down-markets),
after understanding the market and judging the broad index + individual names.

This runs from a DURABLE system cron at Monday open (survives Claude session death).
It is a SAFE GATE-OPENER, not a trader: it verifies the de-leverage completed and
the account is margin-free, THEN flips auto_trade on. The engine itself then does
the real work — market-regime exposure (大盘) + LLM brain with the live Serenity
lens (个股) + 12%/name cap + 10-day min-hold + -5% stop + NO margin (cash-floor
guard). Idempotent and fully guarded: safe to run repeatedly.

Logic:
  - market closed                  → log + exit (do nothing)
  - still on margin, sells pending  → wait, leave auto_trade OFF
  - still on margin, NO pending sells (orders vanished) → RE-SUBMIT de-leverage
                                       sells, leave auto_trade OFF
  - margin cleared (cash >= 0)      → recompute regime + ENABLE auto_trade
"""
import sys, os, requests, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import SessionLocal, get_setting, set_setting

A = "https://api.alpaca.markets"
DELEVERAGE = ["CRCL", "SPY", "HOOD", "AAPL", "META", "MSFT", "GOOGL"]  # full closes


def log(msg):
    print(f"{datetime.datetime.utcnow().isoformat()}Z  {msg}", flush=True)


def main():
    db = SessionLocal()
    H = {"APCA-API-KEY-ID": get_setting(db, "alpaca_api_key", 1, ""),
         "APCA-API-SECRET-KEY": get_setting(db, "alpaca_secret_key", 1, "")}

    clock = requests.get(f"{A}/v2/clock", headers=H, timeout=15).json()
    if not clock.get("is_open"):
        log(f"market CLOSED (next open {clock.get('next_open','?')[:16]}); skip")
        return

    acct = requests.get(f"{A}/v2/account", headers=H, timeout=15).json()
    cash = float(acct["cash"]); eq = float(acct["equity"])
    open_orders = requests.get(f"{A}/v2/orders?status=open&limit=100", headers=H, timeout=15).json()
    pending_sells = [o for o in open_orders if o["side"] == "sell" and o["symbol"] in DELEVERAGE]
    log(f"cash ${cash:.0f} ({cash/eq*100:.0f}% of ${eq:.0f}) | pending de-leverage sells: {len(pending_sells)}")

    # PERMANENTLY disable margin at the BROKER level once cash is positive (user
    # request 2026-06-22). Runs EVERY invocation (independent of auto_trade state)
    # so a one-time failure self-heals next run. max_margin_multiplier=1 →
    # buying_power = cash, borrowing impossible. no_shorting=True → long-only.
    # Only when cash>=0: setting multiplier=1 while still leveraged could force a
    # liquidation.
    if cash >= 0 and not pending_sells:
        try:
            cur = requests.get(f"{A}/v2/account/configurations", headers=H, timeout=15).json()
            if cur.get("max_margin_multiplier") != "1" or not cur.get("no_shorting"):
                cfg = requests.patch(f"{A}/v2/account/configurations", headers=H, timeout=15,
                                     json={"max_margin_multiplier": "1", "no_shorting": True}).json()
                log(f"🔒 margin DISABLED at broker: max_margin_multiplier={cfg.get('max_margin_multiplier')}, "
                    f"no_shorting={cfg.get('no_shorting')}")
            else:
                log("margin already disabled (multiplier=1, no_shorting)")
        except Exception as e:
            log(f"⚠️ could not set cash-only config (will retry next run): {e}")

    # already running? idempotent no-op
    if get_setting(db, "auto_trade_enabled", 1, "false") == "true":
        log("auto_trade already ON; nothing to do")
        return

    # Case 1: margin cleared → open the gate
    if cash >= 0 and not pending_sells:
        try:
            import market_regime
            r = market_regime.main(apply=True)
            log(f"regime={r['regime']} cash_floor={r['cash_floor_pct']}%")
        except Exception as e:
            log(f"regime recompute failed (non-fatal): {e}")
        set_setting(db, "auto_trade_enabled", "true", 1)
        log("✅ DE-LEVERAGE CONFIRMED, MARGIN CLEAR → cash-only locked → auto_trade ENABLED. "
            "Engine operates autonomously (regime exposure + Serenity lens + 12% cap + stops, no margin).")
        return

    # Case 2: sells still working → wait (do not enable yet)
    if pending_sells:
        log("de-leverage sells still pending; leaving auto_trade OFF until they fill")
        return

    # Case 3: still on margin but NO pending sells (orders canceled) → re-submit
    log("⚠️ still on margin with NO pending sells — re-submitting de-leverage")
    pos = {p["symbol"]: p for p in requests.get(f"{A}/v2/positions", headers=H, timeout=15).json()}
    for sym in DELEVERAGE:
        if sym in pos:
            requests.delete(f"{A}/v2/positions/{sym}", headers=H, timeout=15)  # full close, handles fractions
            log(f"  re-closed {sym}")
    log("re-submitted; auto_trade stays OFF this run (re-verify next run)")


if __name__ == "__main__":
    main()
