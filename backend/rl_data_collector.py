"""
Reinforcement Learning Data Collector
Logs every AI signal + trade outcome into a structured dataset
that can later be used to train/fine-tune trading models.

Data schema (per record):
  - state:   market features at time of signal (price, indicators, sentiment)
  - action:  what the AI decided (BUY/SELL/HOLD + confidence)
  - reward:  actual P&L outcome N days after the trade (filled in later)
  - context: event context that was available at signal time
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

import fcntl
import json
import os
import logging
from datetime import datetime, timedelta, date
from collections import defaultdict
from sqlalchemy.orm import Session
from database import get_db, Trade, AISignal, User, get_setting

logger = logging.getLogger(__name__)

RL_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "rl_training_data.jsonl")

# Max BUY/SELL records to back-fill per run (avoids blocking for too long).
# At ~0.3s per symbol batch, 200 symbols ≈ 60s which is acceptable.
_MAX_SYMBOLS_PER_RUN = 200


def _parse_jsonl(path: str) -> list:
    """
    Robustly parse a JSONL file where some lines may contain multiple
    concatenated JSON objects (race-condition artefact from concurrent writes).
    Returns a flat list of all parsed record dicts.
    """
    records = []
    decoder = json.JSONDecoder()
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            idx = 0
            while idx < len(line):
                try:
                    obj, end = decoder.raw_decode(line, idx)
                    records.append(obj)
                    idx = end
                except json.JSONDecodeError:
                    break
    return records


def _write_jsonl(path: str, records: list) -> None:
    """Atomically rewrite the JSONL file (one record per line)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def record_signal_state(
    signal: dict,
    quote: dict,
    indicators: dict,
    event_context: str,
    portfolio_context: str,
    catalysts: list = None,
    active_macros: list = None,
    sector: str = "Other"
):
    """
    Called immediately when an AI signal is generated.
    Appends one record to the JSONL dataset with a file lock to prevent
    concurrent-write corruption.

    Side-effect: pre-computes RL policy scores (production + shadow) on the
    signal dict so they're recorded in the JSONL AND auto_trade() can re-use
    them without recomputing.  Previous version recorded with scores=None
    because auto_trade() set them only AFTER record_signal_state ran.
    """
    # Pre-compute RL scores so the JSONL row has them.  Best-effort: any
    # failure (model not trained yet, missing features) leaves the fields None.
    if signal.get("rl_policy_score") is None and signal.get("rl_shadow_score") is None:
        try:
            import rl_policy_model as _rlpm
            prod_score, shadow_score = _rlpm.predict_with_shadow(
                signal, quote, indicators or {})
            if prod_score is not None:
                signal["rl_policy_score"] = prod_score
            if shadow_score is not None:
                signal["rl_shadow_score"] = shadow_score
        except Exception as _e:
            logger.debug(f"[RL] Could not pre-compute scores: {_e}")

    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": signal.get("symbol"),
        "sector": signal.get("sector", "Other"),
        "action": signal.get("signal", "HOLD"),
        "confidence": signal.get("confidence", 0),
        "target_price": signal.get("target_price"),
        "stop_loss": signal.get("stop_loss"),
        "recommended_weight_pct": signal.get("recommended_weight_pct"),
        "time_horizon": signal.get("time_horizon"),
        "key_factors": signal.get("key_factors", []),
        "model": signal.get("model"),
        "rl_policy_score": signal.get("rl_policy_score"),
        "rl_shadow_score": signal.get("rl_shadow_score"),
        "state": {
            "price": quote.get("current"),
            "change_pct": quote.get("change_pct"),
            "volume": quote.get("volume"),
            "pe_ratio": quote.get("pe_ratio"),
            "market_cap": quote.get("market_cap"),
            "fifty_two_week_low": quote.get("fifty_two_week_low"),
            "fifty_two_week_high": quote.get("fifty_two_week_high"),
            "valuation_gap_pct": quote.get("valuation_gap_pct"),
            "vpa_signal": quote.get("vpa_signal"),
            "vpa_volume_ratio": quote.get("vpa_volume_ratio"),
            "indicators": indicators,
        },
        "event_context_summary": event_context[:500] if event_context else "",
        "portfolio_context": portfolio_context,
        "intelligence_metadata": {
            "catalysts": catalysts or [],
            "macros": active_macros or [],
        },
        "reasoning_summary": signal.get("reasoning", "")[:300],
        "reward_1d": None,
        "reward_3d": None,
        "reward_7d": None,
        "outcome_filled": False,
    }

    try:
        os.makedirs(os.path.dirname(os.path.abspath(RL_DATA_FILE)), exist_ok=True)
        with open(RL_DATA_FILE, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # default=str catches stray datetime / numpy types in indicators
                # dict that occasionally leak from get_technical_indicators().
                # Was failing with "Object of type datetime is not JSON serializable".
                f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"[RL] Failed to write signal record: {e}")


def update_trade_outcomes():
    """
    Back-fill reward fields (reward_1d/3d/7d) for BUY/SELL records that are
    old enough.  Uses yfinance batch history per symbol to minimise API calls.

    Strategy:
    1. Parse the full JSONL (handles concatenated-object lines).
    2. Group pending BUY/SELL records by symbol.
    3. For each symbol fetch one price history covering all signal dates.
    4. Look up the close price N days after each signal.
    5. Rewrite the file atomically when done.
    """
    if not os.path.exists(RL_DATA_FILE):
        return

    import yfinance as yf

    records = _parse_jsonl(RL_DATA_FILE)
    now = datetime.utcnow()

    # Mark all HOLD signals as done immediately (no reward to compute)
    for rec in records:
        if not rec.get("outcome_filled") and rec.get("action") == "HOLD":
            rec["outcome_filled"] = True

    # Group pending non-HOLD records by symbol
    pending_by_sym: dict = defaultdict(list)
    for rec in records:
        if rec.get("outcome_filled"):
            continue
        sym = rec.get("symbol")
        if not sym or not rec.get("state", {}).get("price"):
            continue
        pending_by_sym[sym].append(rec)

    if not pending_by_sym:
        logger.info("[RL] No pending outcome records to fill.")
        return

    symbols_to_process = list(pending_by_sym.keys())[:_MAX_SYMBOLS_PER_RUN]
    logger.info(f"[RL] Back-filling outcomes for {len(symbols_to_process)} symbols "
                f"({sum(len(pending_by_sym[s]) for s in symbols_to_process)} records)")

    updated = 0
    for sym in symbols_to_process:
        sym_records = pending_by_sym[sym]

        # Find earliest signal date for this symbol
        dates = []
        for rec in sym_records:
            try:
                dates.append(datetime.fromisoformat(rec["timestamp"]))
            except Exception:
                pass
        if not dates:
            continue

        earliest = min(dates)
        # Fetch history from earliest signal − 1 day to today + 8 days (covers 7d reward)
        start = (earliest - timedelta(days=1)).strftime("%Y-%m-%d")
        end   = (now + timedelta(days=8)).strftime("%Y-%m-%d")

        try:
            hist = yf.Ticker(sym).history(start=start, end=end)
        except Exception as e:
            logger.debug(f"[RL] yfinance fetch failed for {sym}: {e}")
            continue

        if hist.empty:
            continue

        # Build date → close price lookup (date string → float)
        close_by_date: dict = {}
        for ts, row in hist.iterrows():
            ds = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            close_by_date[ds] = float(row["Close"])

        def nearest_close(target_dt: datetime) -> float | None:
            """Return close price on or after target_dt (up to +3 trading days)."""
            for offset in range(4):
                ds = (target_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                if ds in close_by_date:
                    return close_by_date[ds]
            return None

        for rec in sym_records:
            try:
                signal_time = datetime.fromisoformat(rec["timestamp"])
                # `now` (datetime.utcnow()) is tz-naive; strip any tz info on
                # signal_time to keep `now < target_dt` comparisons safe even
                # for externally-imported tz-aware records.
                if signal_time.tzinfo is not None:
                    signal_time = signal_time.replace(tzinfo=None)
            except Exception:
                continue
            entry_price = rec["state"].get("price")
            if not entry_price:
                continue

            for days, key in [(1, "reward_1d"), (3, "reward_3d"), (7, "reward_7d")]:
                if rec.get(key) is not None:
                    continue
                target_dt = signal_time + timedelta(days=days)
                if now < target_dt:
                    continue
                close = nearest_close(target_dt)
                if close is None:
                    continue
                if not entry_price or entry_price <= 0:
                    continue
                import math
                pct = (close - entry_price) / entry_price * 100
                if not math.isfinite(pct):
                    continue
                if rec["action"] in ("SELL", "SHORT"):
                    pct = -pct
                rec[key] = round(pct, 4)
                updated += 1

            if all(rec.get(k) is not None for k in ["reward_1d", "reward_3d", "reward_7d"]):
                rec["outcome_filled"] = True

    if updated > 0:
        _write_jsonl(RL_DATA_FILE, records)
        logger.info(f"[RL] Wrote {updated} updated reward fields to dataset "
                    f"({len(records)} total records)")
    else:
        logger.info("[RL] No new rewards filled this run (all pending records too recent).")


def get_dataset_stats() -> dict:
    """Return summary stats about the collected training dataset."""
    if not os.path.exists(RL_DATA_FILE):
        return {"total": 0, "with_outcomes": 0, "file": RL_DATA_FILE}

    records = _parse_jsonl(RL_DATA_FILE)
    total = len(records)
    with_outcomes = sum(1 for r in records if r.get("outcome_filled"))
    actions: dict = {}
    for r in records:
        a = r.get("action", "HOLD")
        actions[a] = actions.get(a, 0) + 1

    return {
        "total_records": total,
        "records_with_outcomes": with_outcomes,
        "pending_outcomes": total - with_outcomes,
        "action_distribution": actions,
        "file_path": RL_DATA_FILE,
    }
