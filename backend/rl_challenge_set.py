"""
Challenge Test Set + Error Analysis
====================================
Builds a permanent "hard examples" test set by mining records that the
current production stack consistently gets wrong, then keeps them frozen
forever so future models can be benchmarked against the same difficulty.

How a record qualifies as "hard":
  - Has realised reward_3d (outcome known)
  - Production live system (DeepSeek + XGBoost filter) acted on it AND
    got the direction wrong (e.g. BUY but reward < -2%, or SELL but reward > +2%)
  - Magnitude is large enough that the error mattered (|reward| >= 2%)

Once added, the record is:
  - PERMANENTLY excluded from any training set (via prepare_rl_dataset.py)
  - PERMANENTLY scored as part of validation (every cycle's report)
  - Tagged with the failure pattern (sector, action type, reward magnitude)

Output files in rl_models/:
  - challenge_test_set.jsonl     : the hard examples (append-only)
  - error_analysis.json          : aggregate patterns (sector, catalyst, etc.)
"""
import json
import logging
import math
import os
from collections import Counter, defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR    = os.path.join(REPO_ROOT, "rl_models")
CHALLENGE_FILE = os.path.join(MODELS_DIR, "challenge_test_set.jsonl")
ERROR_REPORT   = os.path.join(MODELS_DIR, "error_analysis.json")

# Magnitude threshold: a wrong call only matters if the move was big enough
MIN_REWARD_MAGNITUDE = 2.0

# Hard cap so the challenge set doesn't grow indefinitely
MAX_CHALLENGE_SIZE = 1000


def _load_existing_fingerprints() -> set:
    """Set of (timestamp+symbol) fingerprints already in challenge set."""
    fps = set()
    if not os.path.exists(CHALLENGE_FILE):
        return fps
    with open(CHALLENGE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                fps.add(f"{r.get('timestamp')}|{r.get('symbol')}")
            except json.JSONDecodeError:
                pass
    return fps


def _is_directional_failure(rec: dict) -> tuple:
    """
    Did the production system get this signal wrong with significant magnitude?
    Returns (is_failure, failure_type) where failure_type is one of:
      "buy_lost"   : BUY signal but actual reward strongly negative
      "sell_lost"  : SELL/SHORT signal but actual reward strongly positive
      "hold_miss"  : HOLD signal but big move happened (missed opportunity)
      None         : not a failure
    """
    reward = rec.get("reward_3d")
    if not isinstance(reward, (int, float)) or not math.isfinite(reward):
        return False, None
    if abs(reward) < MIN_REWARD_MAGNITUDE:
        return False, None

    action = rec.get("action", "HOLD")
    if action in ("BUY", "COVER") and reward < -MIN_REWARD_MAGNITUDE:
        return True, "buy_lost"
    if action in ("SELL", "SHORT") and reward > MIN_REWARD_MAGNITUDE:
        return True, "sell_lost"
    if action == "HOLD" and abs(reward) >= MIN_REWARD_MAGNITUDE * 2:
        return True, "hold_miss"
    return False, None


def mine_hard_examples(records: list, max_new: int = 100) -> dict:
    """
    Scan recent records for production-system failures.  Append new ones to
    challenge_test_set.jsonl with failure-pattern tags.
    Returns a summary dict (added count, failure breakdown, sector breakdown).
    """
    os.makedirs(MODELS_DIR, exist_ok=True)
    existing = _load_existing_fingerprints()

    new_records = []
    failure_types  = Counter()
    sector_breakdown = Counter()
    catalyst_misses  = Counter()

    for rec in records:
        is_fail, ftype = _is_directional_failure(rec)
        if not is_fail:
            continue
        fp = f"{rec.get('timestamp')}|{rec.get('symbol')}"
        if fp in existing:
            continue
        if len(new_records) >= max_new:
            break

        # Tag the record with the failure pattern + capture decision metadata
        tagged = dict(rec)
        tagged["challenge_failure_type"]    = ftype
        tagged["challenge_added_at"]        = datetime.utcnow().isoformat()
        tagged["challenge_prod_reward"]     = rec.get("reward_3d")
        tagged["challenge_prod_score"]      = rec.get("rl_policy_score")
        tagged["challenge_prod_confidence"] = rec.get("confidence")

        new_records.append(tagged)
        failure_types[ftype] += 1
        sector_breakdown[rec.get("sector", "Other")] += 1

        intel = rec.get("intelligence_metadata", {}) or {}
        for cat in intel.get("catalysts", []):
            for kw in cat.get("matched_keywords", []):
                catalyst_misses[kw] += 1

    # Cap total size: if we'd exceed MAX_CHALLENGE_SIZE, drop oldest entries
    total_after = len(existing) + len(new_records)
    if total_after > MAX_CHALLENGE_SIZE:
        # Read all, sort by timestamp, keep newest MAX_CHALLENGE_SIZE - len(new_records)
        all_old = []
        if os.path.exists(CHALLENGE_FILE):
            with open(CHALLENGE_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_old.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        all_old.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        keep_n = MAX_CHALLENGE_SIZE - len(new_records)
        all_old = all_old[:max(0, keep_n)]
        # Rewrite file from scratch
        with open(CHALLENGE_FILE + ".tmp", "w") as f:
            for r in all_old + new_records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(CHALLENGE_FILE + ".tmp", CHALLENGE_FILE)
    else:
        # Append-only
        with open(CHALLENGE_FILE, "a") as f:
            for r in new_records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    summary = {
        "added":           len(new_records),
        "total_in_set":    min(MAX_CHALLENGE_SIZE, total_after),
        "failure_types":   dict(failure_types),
        "by_sector":       dict(sector_breakdown.most_common(10)),
        "top_catalysts":   dict(catalyst_misses.most_common(10)),
        "generated_at":    datetime.utcnow().isoformat(),
    }
    return summary


def analyze_errors(records: list) -> dict:
    """
    Compute a global error-pattern report (without mutating the challenge set).
    Used for the /api/rl/errors endpoint.
    """
    by_sector_total   = Counter()
    by_sector_correct = Counter()
    by_action_total   = Counter()
    by_action_correct = Counter()
    by_confidence_total = defaultdict(int)
    by_confidence_correct = defaultdict(int)

    for rec in records:
        reward = rec.get("reward_3d")
        if not isinstance(reward, (int, float)) or not math.isfinite(reward):
            continue
        if abs(reward) < MIN_REWARD_MAGNITUDE:
            continue

        action = rec.get("action", "HOLD")
        sector = rec.get("sector", "Other")
        conf   = rec.get("confidence", 0) or 0
        # Bucket confidence 0-1 into 0.0-0.2, 0.2-0.4, ...
        bucket = f"{int(conf * 5) * 20}-{int(conf * 5) * 20 + 20}"

        if action in ("BUY", "COVER"):
            correct = reward > 0
        elif action in ("SELL", "SHORT"):
            correct = reward < 0
        else:
            correct = abs(reward) < MIN_REWARD_MAGNITUDE

        by_sector_total[sector]   += 1
        by_action_total[action]   += 1
        by_confidence_total[bucket] += 1
        if correct:
            by_sector_correct[sector] += 1
            by_action_correct[action] += 1
            by_confidence_correct[bucket] += 1

    def _accuracy(correct_dict, total_dict):
        return {
            k: {
                "samples":  total_dict[k],
                "correct":  correct_dict.get(k, 0),
                "accuracy": round(correct_dict.get(k, 0) / total_dict[k] * 100, 2)
                            if total_dict[k] else 0,
            }
            for k in total_dict
        }

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "by_sector":     _accuracy(by_sector_correct, by_sector_total),
        "by_action":     _accuracy(by_action_correct, by_action_total),
        "by_confidence": _accuracy(by_confidence_correct, by_confidence_total),
    }

    # Sort sector by worst accuracy (these are the model's weak spots)
    sorted_sectors = sorted(report["by_sector"].items(),
                            key=lambda x: x[1]["accuracy"])
    report["weakest_sectors"] = [
        {"sector": s, **info} for s, info in sorted_sectors[:5]
    ]

    # Persist for inspection
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(ERROR_REPORT + ".tmp", "w") as f:
        json.dump(report, f, indent=2)
    os.replace(ERROR_REPORT + ".tmp", ERROR_REPORT)

    return report


def load_challenge_set() -> list:
    """Return all records in the challenge set."""
    if not os.path.exists(CHALLENGE_FILE):
        return []
    records = []
    with open(CHALLENGE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def get_status() -> dict:
    """Snapshot for the dashboard API."""
    records = load_challenge_set()
    return {
        "total":        len(records),
        "by_failure_type": dict(Counter(r.get("challenge_failure_type", "?") for r in records)),
        "by_sector":       dict(Counter(r.get("sector", "Other") for r in records).most_common(10)),
        "file":         CHALLENGE_FILE,
        "max_size":     MAX_CHALLENGE_SIZE,
        "min_magnitude_pct": MIN_REWARD_MAGNITUDE,
    }
