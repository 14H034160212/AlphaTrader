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

import json
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import get_db, Trade, AISignal, User, get_setting

logger = logging.getLogger(__name__)

RL_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "rl_training_data.jsonl")


def record_signal_state(signal: dict, quote: dict, indicators: dict,
                         event_context: str, portfolio_context: str):
    """
    Called immediately when an AI signal is generated.
    Saves the full state + action to the JSONL dataset.
    The reward field is left as null and filled in later by update_trade_outcomes().
    """
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": signal.get("symbol"),
        # ── Action (what AI decided) ──────────────────────────────────────────
        "action": signal.get("signal", "HOLD"),
        "confidence": signal.get("confidence", 0),
        "target_price": signal.get("target_price"),
        "stop_loss": signal.get("stop_loss"),
        "recommended_weight_pct": signal.get("recommended_weight_pct"),
        "time_horizon": signal.get("time_horizon"),
        "key_factors": signal.get("key_factors", []),
        "model": signal.get("model"),
        # ── State (market features at decision time) ─────────────────────────
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
        # ── Context ───────────────────────────────────────────────────────────
        "event_context_summary": event_context[:500] if event_context else "",
        "portfolio_context": portfolio_context,
        "reasoning_summary": signal.get("reasoning", "")[:300],
        # ── Outcome (filled in later) ─────────────────────────────────────────
        "reward_1d": None,   # % P&L after 1 day
        "reward_3d": None,   # % P&L after 3 days
        "reward_7d": None,   # % P&L after 7 days
        "outcome_filled": False,
    }

    try:
        os.makedirs(os.path.dirname(os.path.abspath(RL_DATA_FILE)), exist_ok=True)
        with open(RL_DATA_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"[RL] Failed to write signal record: {e}")


def update_trade_outcomes():
    """
    Scan the JSONL file and fill in reward fields for records that are old enough.
    Uses yfinance to fetch actual price N days after the signal.
    Run periodically (e.g., once per day) to backfill outcomes.
    """
    import yfinance as yf

    if not os.path.exists(RL_DATA_FILE):
        return

    records = []
    with open(RL_DATA_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    updated = 0
    now = datetime.utcnow()

    for rec in records:
        if rec.get("outcome_filled"):
            continue
        if rec["action"] == "HOLD":
            rec["outcome_filled"] = True
            continue

        signal_time = datetime.fromisoformat(rec["timestamp"])
        symbol = rec["symbol"]
        entry_price = rec["state"].get("price")
        if not entry_price:
            continue

        # Fill rewards for 1d, 3d, 7d if enough time has passed
        for days, key in [(1, "reward_1d"), (3, "reward_3d"), (7, "reward_7d")]:
            if rec.get(key) is not None:
                continue
            target_dt = signal_time + timedelta(days=days)
            if now < target_dt:
                continue  # Not enough time has passed yet
            try:
                end_dt = target_dt + timedelta(days=2)
                hist = yf.Ticker(symbol).history(
                    start=target_dt.strftime("%Y-%m-%d"),
                    end=end_dt.strftime("%Y-%m-%d")
                )
                if hist.empty:
                    continue
                close_price = float(hist["Close"].iloc[0])
                pct = (close_price - entry_price) / entry_price * 100
                # Invert reward for SHORT/SELL signals
                if rec["action"] in ("SELL", "SHORT"):
                    pct = -pct
                rec[key] = round(pct, 4)
                updated += 1
            except Exception as e:
                logger.debug(f"[RL] Could not fetch {symbol} outcome for {key}: {e}")

        # Mark as fully filled if all rewards are populated
        if all(rec.get(k) is not None for k in ["reward_1d", "reward_3d", "reward_7d"]):
            rec["outcome_filled"] = True

    if updated > 0:
        # Rewrite the file with updated records
        with open(RL_DATA_FILE, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        logger.info(f"[RL] Updated {updated} outcome records in training dataset")


def get_dataset_stats():
    """Return summary stats about the collected training dataset."""
    if not os.path.exists(RL_DATA_FILE):
        return {"total": 0, "with_outcomes": 0, "file": RL_DATA_FILE}

    total = 0
    with_outcomes = 0
    actions = {}

    with open(RL_DATA_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                total += 1
                if rec.get("outcome_filled"):
                    with_outcomes += 1
                action = rec.get("action", "HOLD")
                actions[action] = actions.get(action, 0) + 1
            except Exception:
                pass

    return {
        "total_records": total,
        "records_with_outcomes": with_outcomes,
        "pending_outcomes": total - with_outcomes,
        "action_distribution": actions,
        "file_path": RL_DATA_FILE,
    }
