#!/usr/bin/env python3
"""market_regime.py — adaptive exposure engine.

User's mandate (2026-06-19): "在市场好的时候赚更多，市场不好的时候少亏或不亏"
— scale risk exposure UP in healthy markets, DOWN in stressed ones.

How it works
------------
Reads SPY daily bars from Alpaca (free IEX feed), scores the market "regime"
from trend / momentum / volatility / drawdown, and maps it to a target
``cash_reserve_pct``. The trading engine already reads that setting on every
buy via ``get_cash_reserve_status()`` and the execute_buy cash-floor guard, so
simply writing the setting makes the whole engine adapt — no engine code change,
no restart. Run daily pre-open from cron.

  RISK_ON   → cash floor 20%  (deploy up to 80%) — lean in
  NEUTRAL   → cash floor 40%  (deploy up to 60%) — balanced
  RISK_OFF  → cash floor 65%  (deploy up to 35%) — defend; also trims excess

Survival-first ([[user_living_money_risk_posture]]): this only ever ADDS
caution automatically. Deploying more in RISK_ON is still bounded by
MAX_POSITION_PCT (12%/name) and Serenity conviction — it never uses margin
(get_cash_balance returns real cash; see project_margin_incident_20260619).
"""
import sys, math, statistics, datetime
import requests

sys.path.insert(0, __import__("os").path.dirname(__file__))
from database import SessionLocal, get_setting, set_setting  # noqa: E402

_DATA = "https://data.alpaca.markets/v2/stocks/{sym}/bars"

# regime → (cash_floor_pct, label). Higher floor = less exposure.
_FLOOR = {"RISK_ON": 20, "NEUTRAL": 40, "RISK_OFF": 65}


def _fetch_closes(sym, key, sec, days=300):
    start = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    end = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    H = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    r = requests.get(_DATA.format(sym=sym), headers=H, timeout=20, params={
        "timeframe": "1Day", "start": start, "end": end,
        "limit": 300, "feed": "iex", "adjustment": "all"})
    r.raise_for_status()   # surface 401/403/429 instead of silently returning []
    return [b["c"] for b in (r.json().get("bars") or [])]


def assess_regime(key, sec):
    """Return {regime, score, cash_floor_pct, metrics{...}}. Falls back to
    NEUTRAL on any data failure (never leaves the engine ungated)."""
    try:
        c = _fetch_closes("SPY", key, sec)
        if len(c) < 60:
            raise ValueError(f"only {len(c)} bars")
    except Exception as e:
        # Data feed down (bad keys / 429 / outage). Do NOT silently reset to
        # NEUTRAL — during a crash that would leave the engine at the 40% floor
        # deploying into the decline. Signal failure so main() keeps the LAST
        # regime (and a real RISK_OFF set earlier stays in force).
        return {"regime": "UNKNOWN", "score": 0, "cash_floor_pct": None,
                "metrics": {"error": str(e)}}

    last = c[-1]
    ma50 = statistics.mean(c[-50:])
    ma200 = statistics.mean(c[-200:]) if len(c) >= 200 else statistics.mean(c)
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    rv20 = statistics.pstdev(rets[-20:]) * math.sqrt(252) * 100
    mom10 = (c[-1] / c[-11] - 1) * 100
    dd60 = (last / max(c[-60:]) - 1) * 100  # drawdown from 60-day high

    # ── score: +1 favourable / −1 adverse on each axis ──
    score = 0
    score += 1 if last > ma50 else -1
    score += 1 if last > ma200 else -1
    score += 1 if mom10 > 0 else -1
    score += 1 if rv20 < 20 else (-1 if rv20 > 30 else 0)

    # hard risk-off override: a real correction (>12% off the 60d high) forces
    # defense regardless of the trailing-average trend, which lags crashes.
    if dd60 <= -12:
        regime = "RISK_OFF"
    elif score >= 3:
        regime = "RISK_ON"
    elif score <= -2:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    return {
        "regime": regime, "score": score, "cash_floor_pct": _FLOOR[regime],
        "metrics": {
            "spy": round(last, 2), "vs_50dma_pct": round((last / ma50 - 1) * 100, 2),
            "vs_200dma_pct": round((last / ma200 - 1) * 100, 2),
            "mom10_pct": round(mom10, 2), "rv20_ann_pct": round(rv20, 1),
            "drawdown_60d_pct": round(dd60, 2),
        },
    }


def main(apply=True):
    db = SessionLocal()
    key = get_setting(db, "alpaca_api_key", 1, "")
    sec = get_setting(db, "alpaca_secret_key", 1, "")
    a = assess_regime(key, sec)
    m = a["metrics"]
    print(f"REGIME {a['regime']} (score {a['score']}) → cash_floor {a['cash_floor_pct']}% | {m}")
    if apply:
        if a["regime"] == "UNKNOWN" or a["cash_floor_pct"] is None:
            # keep the last regime/floor rather than overwrite on a data outage
            print(f"  data feed unavailable ({m.get('error')}); KEEPING last "
                  f"cash_reserve_pct={get_setting(db, 'cash_reserve_pct', 1, '?')}, "
                  f"market_regime={get_setting(db, 'market_regime', 1, '?')}")
        else:
            prev = get_setting(db, "cash_reserve_pct", 1, "?")
            set_setting(db, "cash_reserve_pct", str(a["cash_floor_pct"]), 1)
            set_setting(db, "market_regime", a["regime"], 1)
            print(f"  applied: cash_reserve_pct {prev} → {a['cash_floor_pct']}; market_regime={a['regime']}")
    return a


if __name__ == "__main__":
    main(apply="--dry" not in sys.argv)
