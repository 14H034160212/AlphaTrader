"""
Prepare RL training dataset from AlphaTrader JSONL records.

Converts rl_training_data.jsonl into a Hugging Face dataset suitable for:
  - Reward-weighted SFT (offline RL, simpler — run first)
  - GRPO online RL (once SFT adapter is validated)

Output format (each row):
  {
    "prompt":  "<system>...<user>Market analysis request...",
    "response": "The AI's original reasoning/signal text",
    "reward":   3.14,   # reward_3d in %
    "weight":   1.0,    # sample weight for reward-weighted SFT
  }

Usage:
  python prepare_rl_dataset.py \
      --rl_data ../rl_training_data.jsonl \
      --output ./rl_sft_dataset \
      --min_reward_abs 0.5   # drop near-zero rewards (label noise)
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


SYSTEM_PROMPT = (
    "You are AlphaTrader, an expert AI stock trading analyst. "
    "Given the market state and context, produce a structured trading signal: "
    "BUY / SELL / HOLD with confidence (0-1), target price, stop loss, "
    "recommended portfolio weight %, time horizon, and detailed reasoning. "
    "Prioritize large-cap, well-known companies. Ignore geopolitical noise unless "
    "directly impacting fundamentals."
)


def _format_state(rec: dict) -> str:
    state = rec.get("state", {}) or {}
    indicators = state.get("indicators", {}) or {}
    lines = [
        f"Symbol: {rec.get('symbol', 'UNKNOWN')}",
        f"Sector: {rec.get('sector', 'Other')}",
        f"Price: ${state.get('price', 'N/A')}",
        f"Day change: {state.get('change_pct', 'N/A')}%",
        f"Volume: {state.get('volume', 'N/A')}",
        f"PE ratio: {state.get('pe_ratio', 'N/A')}",
        f"52w low: {state.get('fifty_two_week_low', 'N/A')}  52w high: {state.get('fifty_two_week_high', 'N/A')}",
        f"Valuation gap: {state.get('valuation_gap_pct', 'N/A')}%",
        f"VPA signal: {state.get('vpa_signal', 'N/A')}  VPA volume ratio: {state.get('vpa_volume_ratio', 'N/A')}",
        f"RSI: {indicators.get('rsi', 'N/A')}  MACD: {indicators.get('macd', 'N/A')}  ATR: {indicators.get('atr', 'N/A')}",
        f"Portfolio context: {rec.get('portfolio_context', 'N/A')}",
        f"Event context: {rec.get('event_context_summary', '')}",
    ]

    intel = rec.get("intelligence_metadata", {}) or {}
    catalysts = intel.get("catalysts", [])
    if catalysts:
        kws = [kw for c in catalysts for kw in c.get("matched_keywords", [])]
        lines.append(f"Catalyst keywords: {', '.join(kws[:10])}")

    macros = intel.get("macros", [])
    if macros:
        ids = [m.get("scenario_id", "") for m in macros]
        lines.append(f"Active macro scenarios: {', '.join(ids[:5])}")

    return "\n".join(lines)


def _format_response(rec: dict) -> str:
    action     = rec.get("action", "HOLD")
    confidence = rec.get("confidence", 0)
    target     = rec.get("target_price")
    stop       = rec.get("stop_loss")
    weight     = rec.get("recommended_weight_pct")
    horizon    = rec.get("time_horizon", "")
    factors    = rec.get("key_factors", [])
    reasoning  = rec.get("reasoning_summary", "")

    lines = [
        f"Signal: {action}",
        f"Confidence: {confidence:.2f}",
        f"Target price: {target}",
        f"Stop loss: {stop}",
        f"Recommended weight: {weight}%",
        f"Time horizon: {horizon}",
    ]
    if factors:
        lines.append(f"Key factors: {'; '.join(str(f) for f in factors[:5])}")
    if reasoning:
        lines.append(f"Reasoning: {reasoning}")
    return "\n".join(lines)


def _reward_weight(reward: float, action: str) -> float:
    """
    Sample weight for reward-weighted SFT loss.
    - Correct strong signals (large positive reward for BUY, large negative for SELL) → high weight
    - Wrong signals → weight near 0 (still included but barely trained on)
    - Near-zero rewards → filtered out upstream (label noise)
    """
    if action in ("BUY", "COVER"):
        correctness = reward          # positive reward = correct BUY
    elif action in ("SELL", "SHORT"):
        correctness = -reward         # negative reward = correct SELL
    else:
        return 0.0                    # HOLD: skip in training

    # Sigmoid-like scaling: large correct signals → ~2.0, wrong signals → ~0.1
    raw = math.tanh(correctness / 3.0)   # tanh(1%)→0.32  tanh(3%)→0.90
    return max(0.05, raw + 1.0)           # range [0.05, 2.0]


def build_dataset(
    rl_data_path: str,
    output_dir: str,
    min_reward_abs: float = 0.5,
    val_split: float = 0.05,
    exclude_last_days: int = 7,
    exclude_challenge_set: bool = True,
) -> dict:
    """
    Build SFT dataset from RL JSONL.

    Data-leakage guards:
      - exclude_last_days=N: drop records timestamped within the last N days.
        These records are reserved as the pipeline's holdout test set and must
        NEVER appear in training.  Default 7 = matches rl_validation cutoff.
      - exclude_challenge_set=True: drop any record whose fingerprint is in
        rl_models/challenge_test_set.jsonl (permanently-frozen hard examples).
    """
    from datetime import datetime, timedelta
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
    import rl_data_collector as _rl
    records = _rl._parse_jsonl(rl_data_path)

    # Cutoff for the rolling holdout
    cutoff = datetime.utcnow() - timedelta(days=exclude_last_days) if exclude_last_days > 0 else None

    # Permanent challenge-set fingerprints (timestamp + symbol)
    challenge_fingerprints = set()
    challenge_path = os.path.join(os.path.dirname(__file__), "..", "rl_models", "challenge_test_set.jsonl")
    if exclude_challenge_set and os.path.exists(challenge_path):
        with open(challenge_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    challenge_fingerprints.add(f"{r.get('timestamp')}|{r.get('symbol')}")
                except json.JSONDecodeError:
                    pass
        print(f"[Leakage Guard] Excluding {len(challenge_fingerprints)} challenge-set records")

    rows = []
    skipped = 0
    skipped_holdout = 0
    skipped_challenge = 0
    for rec in records:
        reward = rec.get("reward_3d")
        if reward is None or not isinstance(reward, (int, float)) or not math.isfinite(reward):
            skipped += 1
            continue
        if abs(reward) < min_reward_abs:
            skipped += 1
            continue
        action = rec.get("action", "HOLD")
        if action == "HOLD":
            skipped += 1
            continue

        # Data-leakage guard 1: drop rolling-holdout records
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(rec["timestamp"])
                if ts >= cutoff:
                    skipped_holdout += 1
                    continue
            except Exception:
                pass

        # Data-leakage guard 2: drop permanent challenge-set records
        fp = f"{rec.get('timestamp')}|{rec.get('symbol')}"
        if fp in challenge_fingerprints:
            skipped_challenge += 1
            continue

        prompt   = f"<|system|>\n{SYSTEM_PROMPT}\n<|user|>\n{_format_state(rec)}"
        response = _format_response(rec)
        weight   = _reward_weight(reward, action)

        rows.append({
            "prompt":    prompt,
            "response":  response,
            "reward":    round(reward, 4),
            "weight":    round(weight, 4),
            "symbol":    rec.get("symbol"),
            "action":    action,
            "timestamp": rec.get("timestamp"),
        })

    print(f"Kept {len(rows)} rows, skipped {skipped} (no reward / near-zero / HOLD), "
          f"{skipped_holdout} (rolling holdout last {exclude_last_days}d), "
          f"{skipped_challenge} (permanent challenge set)")

    # Shuffle and split
    import random
    random.seed(42)
    random.shuffle(rows)
    val_n  = max(1, int(len(rows) * val_split))
    train  = rows[val_n:]
    val    = rows[:val_n]

    os.makedirs(output_dir, exist_ok=True)
    for split, data in [("train", train), ("val", val)]:
        path = os.path.join(output_dir, f"{split}.jsonl")
        with open(path, "w") as f:
            for row in data:
                f.write(json.dumps(row) + "\n")
        print(f"  {split}: {len(data)} rows → {path}")

    stats = {
        "total_rows": len(rows),
        "train_rows": len(train),
        "val_rows":   len(val),
        "skipped":    skipped,
        "output_dir": output_dir,
        "reward_stats": {
            "mean":  round(sum(r["reward"] for r in rows) / len(rows), 4) if rows else 0,
            "min":   round(min(r["reward"] for r in rows), 4) if rows else 0,
            "max":   round(max(r["reward"] for r in rows), 4) if rows else 0,
        }
    }
    print(f"\nDataset stats: {json.dumps(stats, indent=2)}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rl_data",        default="../rl_training_data.jsonl")
    parser.add_argument("--output",         default="./rl_sft_dataset")
    parser.add_argument("--min_reward_abs", type=float, default=0.5)
    parser.add_argument("--val_split",      type=float, default=0.05)
    parser.add_argument("--exclude_last_days", type=int, default=7,
                        help="Drop records from the last N days (reserved as pipeline holdout)")
    parser.add_argument("--exclude_challenge_set", action="store_true", default=True,
                        help="Drop records listed in rl_models/challenge_test_set.jsonl")
    args = parser.parse_args()

    build_dataset(
        rl_data_path          = args.rl_data,
        output_dir            = args.output,
        min_reward_abs        = args.min_reward_abs,
        val_split             = args.val_split,
        exclude_last_days     = args.exclude_last_days,
        exclude_challenge_set = args.exclude_challenge_set,
    )
