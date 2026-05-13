"""
RL Model Validation
====================
Holdout-based validation for both XGBoost (Path 1) and LoRA (Path 2) models.

Strategy:
  - Hold out the most recent `holdout_days` of records (e.g. last 7 days)
  - Train candidate model on records OLDER than the cutoff
  - Score the candidate on the holdout set
  - Compare against the currently-deployed production model
  - Return a structured decision: "promote" | "shadow" | "reject"

Metrics:
  - RMSE: how close predicted reward is to actual reward
  - Directional accuracy: prediction sign matches actual sign
  - Sharpe-like ratio: mean(predicted * actual) / std(predicted) — proxy for
    how well the predicted score *ranks* trades

Comparison logic:
  - Directional accuracy improvement >= 2.0pp  → promote
  - Improvement in [0.0pp, 2.0pp)              → shadow (run 7 days in parallel)
  - Worse than baseline                         → reject
"""
import json
import logging
import math
import os
import pickle
from datetime import datetime, timedelta

import numpy as np

logger = logging.getLogger(__name__)

RL_DATA_FILE   = os.path.join(os.path.dirname(__file__), "..", "rl_training_data.jsonl")
MODELS_DIR     = os.path.join(os.path.dirname(__file__), "..", "rl_models")
REGISTRY_FILE  = os.path.join(MODELS_DIR, "registry.json")

# Promotion thresholds
DIR_ACC_PROMOTE_THRESHOLD = 2.0   # percentage points improvement → auto-promote
DIR_ACC_SHADOW_THRESHOLD  = 0.0   # if >0 but < promote → shadow mode
MIN_HOLDOUT_SAMPLES       = 50


def _ensure_dirs():
    os.makedirs(MODELS_DIR, exist_ok=True)


# ──────────────────────────────────────────────
# Holdout split
# ──────────────────────────────────────────────

def split_train_holdout(records: list, holdout_days: int = 7) -> tuple:
    """
    Split records into (train, holdout) based on signal timestamp.
    Holdout = records from the last `holdout_days`.
    Train   = records older than the cutoff.
    Only records with `reward_3d` filled are considered.
    """
    cutoff = datetime.utcnow() - timedelta(days=holdout_days)
    train, holdout = [], []
    for rec in records:
        reward = rec.get("reward_3d")
        if reward is None or not isinstance(reward, (int, float)) or not math.isfinite(reward):
            continue
        try:
            ts = datetime.fromisoformat(rec["timestamp"])
        except Exception:
            continue
        if ts >= cutoff:
            holdout.append(rec)
        else:
            train.append(rec)
    return train, holdout


# ──────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────

def compute_xgb_metrics(model, holdout_records: list) -> dict | None:
    """
    Score an XGBoost model on holdout records.
    Returns {"samples", "rmse", "directional_accuracy", "rank_corr"}.
    """
    import rl_policy_model as _rlpm
    X, y = [], []
    for rec in holdout_records:
        feats = _rlpm._extract_features(rec)
        if feats is None:
            continue
        X.append(feats)
        y.append(float(rec["reward_3d"]))

    if len(X) < MIN_HOLDOUT_SAMPLES:
        logger.warning(f"[Validate] Only {len(X)} holdout samples — skipping")
        return None

    X_np = np.array(X, dtype=np.float32)
    y_np = np.array(y, dtype=np.float32)

    try:
        pred = model.predict(X_np)
    except Exception as e:
        logger.error(f"[Validate] Prediction failed: {e}")
        return None

    pred = np.array(pred, dtype=np.float32)
    rmse = float(np.sqrt(np.mean((pred - y_np) ** 2)))

    # Directional accuracy: sign(pred) == sign(actual), ignoring near-zero
    nonzero_mask = (np.abs(y_np) > 0.5)  # ignore noisy near-zero outcomes
    if nonzero_mask.sum() > 0:
        dir_correct = (np.sign(pred[nonzero_mask]) == np.sign(y_np[nonzero_mask])).sum()
        dir_acc     = float(dir_correct) / float(nonzero_mask.sum()) * 100
    else:
        dir_acc = 0.0

    # Rank correlation (Spearman approximation): correlates predicted rank with actual rank
    try:
        from scipy.stats import spearmanr
        rank_corr, _ = spearmanr(pred, y_np)
        rank_corr = float(rank_corr) if math.isfinite(rank_corr) else 0.0
    except ImportError:
        # fallback: pearson on ranks
        pred_rank = np.argsort(np.argsort(pred))
        y_rank    = np.argsort(np.argsort(y_np))
        rank_corr = float(np.corrcoef(pred_rank, y_rank)[0, 1])
        if not math.isfinite(rank_corr):
            rank_corr = 0.0

    return {
        "samples": int(len(X)),
        "rmse": round(rmse, 4),
        "directional_accuracy": round(dir_acc, 2),
        "rank_corr": round(rank_corr, 4),
    }


def compute_baseline_metrics(holdout_records: list) -> dict:
    """
    Trivial baseline: predict the mean reward of the training set.
    Used as a sanity check — any useful model should beat this.
    """
    rewards = [float(r["reward_3d"]) for r in holdout_records
               if r.get("reward_3d") is not None]
    if not rewards:
        return {"samples": 0, "rmse": 0.0, "directional_accuracy": 0.0}
    rmse = float(np.std(rewards))   # mean predictor's RMSE = stddev of targets
    n_pos = sum(1 for r in rewards if r > 0)
    dir_acc = float(n_pos) / len(rewards) * 100   # always-positive predictor
    return {"samples": len(rewards), "rmse": round(rmse, 4),
            "directional_accuracy": round(dir_acc, 2), "rank_corr": 0.0}


# ──────────────────────────────────────────────
# Decision logic
# ──────────────────────────────────────────────

def decide_promotion(new_metrics: dict, prod_metrics: dict | None) -> dict:
    """
    Compare candidate metrics against production metrics.
    Returns: {"decision": "promote"|"shadow"|"reject", "reason": str, "delta": dict}
    """
    if new_metrics is None:
        return {"decision": "reject", "reason": "no metrics from candidate", "delta": {}}

    if prod_metrics is None:
        # No production model yet → promote anything that beats trivial baseline
        return {"decision": "promote",
                "reason": "no production model — first deployment",
                "delta": new_metrics}

    delta_dir_acc = new_metrics["directional_accuracy"] - prod_metrics["directional_accuracy"]
    delta_rmse    = new_metrics["rmse"] - prod_metrics["rmse"]
    delta_rank    = new_metrics["rank_corr"] - prod_metrics["rank_corr"]

    delta = {
        "directional_accuracy_pp": round(delta_dir_acc, 2),
        "rmse":                    round(delta_rmse, 4),
        "rank_corr":               round(delta_rank, 4),
    }

    if delta_dir_acc >= DIR_ACC_PROMOTE_THRESHOLD and delta_rmse <= 0:
        return {"decision": "promote",
                "reason": f"+{delta_dir_acc:.1f}pp dir-acc, RMSE {delta_rmse:+.3f}",
                "delta": delta}
    if delta_dir_acc > DIR_ACC_SHADOW_THRESHOLD:
        return {"decision": "shadow",
                "reason": f"+{delta_dir_acc:.1f}pp dir-acc, needs validation",
                "delta": delta}
    return {"decision": "reject",
            "reason": f"{delta_dir_acc:+.1f}pp dir-acc, RMSE {delta_rmse:+.3f}",
            "delta": delta}


# ──────────────────────────────────────────────
# Model registry
# ──────────────────────────────────────────────

def load_registry() -> dict:
    _ensure_dirs()
    if not os.path.exists(REGISTRY_FILE):
        return {"production": None, "shadow": None, "versions": []}
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[Registry] Load failed: {e}")
        return {"production": None, "shadow": None, "versions": []}


def save_registry(registry: dict) -> None:
    _ensure_dirs()
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(registry, f, indent=2)
    os.replace(tmp, REGISTRY_FILE)


def register_version(version: str, kind: str, metrics: dict, decision: dict, path: str) -> None:
    """Append a new model version to the registry."""
    registry = load_registry()
    entry = {
        "version":   version,
        "kind":      kind,           # "xgboost" or "lora"
        "created_at": datetime.utcnow().isoformat(),
        "metrics":   metrics,
        "decision":  decision,
        "path":      path,
    }
    registry["versions"].append(entry)
    if decision["decision"] == "promote":
        registry["production"] = version
        registry["shadow"]     = None   # promoted overrides any pending shadow
    elif decision["decision"] == "shadow":
        registry["shadow"] = version
    save_registry(registry)
    logger.info(f"[Registry] Registered {kind} {version}: {decision['decision']} ({decision['reason']})")
