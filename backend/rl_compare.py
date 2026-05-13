"""
Fair Multi-Method Comparison Harness
=====================================
Scores every trading-signal method against the SAME holdout records using
the SAME decision rule, so XGBoost / LoRA / Production / Baseline can be
compared apples-to-apples.

Unified decision rule for ALL methods:
  - "BUY"-like signal & realised reward > 0      → win
  - "SELL"-like signal & realised reward < 0     → win
  - "HOLD"-like signal & |reward| < 1%           → win (we correctly stayed out)
  - Otherwise                                     → loss

Each method must produce one of {BUY, SELL, HOLD} per holdout record.
Methods scored side-by-side:

  1. XGBoost candidate    : threshold its predicted reward into action
                              pred >  +1.0% → BUY
                              pred <  -1.0% → SELL
                              else          → HOLD
  2. XGBoost production   : same model class, same threshold rule
  3. LoRA adapter         : parse generated text for action
  4. Production live      : action already in JSONL (DeepSeek+XGBoost filter)
  5. Trivial always-BUY   : floor for context
  6. Trivial always-HOLD  : opposite floor

Metrics per method:
  - win_rate                 : wins / total (excluding skipped)
  - directional_accuracy_pp  : same as win_rate but only on non-HOLD records
  - mean_realised_reward     : avg signed reward if you'd followed the signal
  - sharpe_proxy             : mean_reward / std_reward
  - total_signals            : how many decisions made
"""
import logging
import math
import os
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

XGB_BUY_THRESHOLD  = 1.0   # predicted reward > +1.0% → BUY
XGB_SELL_THRESHOLD = -1.0  # predicted reward < -1.0% → SELL


# ──────────────────────────────────────────────
# Action derivation
# ──────────────────────────────────────────────

def _xgb_predict_action(model, rec: dict) -> tuple:
    """Run XGBoost model on record, threshold output → action."""
    import rl_policy_model as _rlpm
    feats = _rlpm._extract_features(rec)
    if feats is None:
        return "SKIP", None
    try:
        pred = float(model.predict(np.array([feats], dtype=np.float32))[0])
        if not math.isfinite(pred):
            return "SKIP", None
    except Exception:
        return "SKIP", None

    if pred > XGB_BUY_THRESHOLD:
        return "BUY", round(pred, 3)
    if pred < XGB_SELL_THRESHOLD:
        return "SELL", round(pred, 3)
    return "HOLD", round(pred, 3)


def _production_action(rec: dict) -> tuple:
    """Pull the action from JSONL — what the live system actually decided."""
    action = rec.get("action", "HOLD")
    if action in ("BUY", "COVER"):
        return "BUY", rec.get("rl_policy_score")
    if action in ("SELL", "SHORT"):
        return "SELL", rec.get("rl_policy_score")
    return "HOLD", rec.get("rl_policy_score")


# ──────────────────────────────────────────────
# Unified scoring
# ──────────────────────────────────────────────

def score_method(actions: list, rewards: list, method_name: str) -> dict:
    """
    actions[i]  : "BUY" | "SELL" | "HOLD" | "SKIP"
    rewards[i]  : realised reward_3d % for record i
    """
    wins = 0
    total = 0
    realised_rewards = []
    n_buy, n_sell, n_hold = 0, 0, 0

    for action, reward in zip(actions, rewards):
        if action == "SKIP":
            continue
        if not math.isfinite(reward):
            continue
        total += 1
        if action == "BUY":
            n_buy += 1
            realised_rewards.append(reward)
            if reward > 0:
                wins += 1
        elif action == "SELL":
            n_sell += 1
            realised_rewards.append(-reward)   # short profits when reward < 0
            if reward < 0:
                wins += 1
        elif action == "HOLD":
            n_hold += 1
            # HOLD = no trade — 0 P&L; only counts as a "correct" call if
            # the move would have been small (we didn't miss anything big)
            if abs(reward) < 1.0:
                wins += 1

    # Non-HOLD directional accuracy
    acted = n_buy + n_sell
    acted_wins = sum(1 for a, r in zip(actions, rewards)
                     if a == "BUY"  and math.isfinite(r) and r > 0) + \
                 sum(1 for a, r in zip(actions, rewards)
                     if a == "SELL" and math.isfinite(r) and r < 0)
    dir_acc = round(acted_wins / acted * 100, 2) if acted else None

    mean_reward = round(float(np.mean(realised_rewards)), 4) if realised_rewards else 0.0
    std_reward  = round(float(np.std(realised_rewards)), 4)  if len(realised_rewards) > 1 else 0.0
    sharpe      = round(mean_reward / std_reward, 4) if std_reward > 0 else 0.0

    return {
        "method":               method_name,
        "total_signals":        total,
        "buy":                  n_buy,
        "sell":                 n_sell,
        "hold":                 n_hold,
        "win_rate":             round(wins / total * 100, 2) if total else None,
        "directional_accuracy": dir_acc,
        "mean_realised_reward": mean_reward,
        "sharpe_proxy":         sharpe,
    }


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

def run_comparison(holdout_days: int = 7,
                   include_lora: bool = True,
                   max_lora_samples: int = 80,
                   include_challenge_set: bool = True) -> dict:
    """
    Build the holdout, score every available method, return a structured
    report.  When include_challenge_set=True, also scores every method on
    the permanent challenge_test_set.jsonl (frozen hard examples) — this
    is the most rigorous benchmark since those records were never in any
    model's training set.
    """
    import rl_data_collector as _rl
    import rl_validation as _val
    import rl_policy_model as _rlpm

    records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
    _, holdout = _val.split_train_holdout(records, holdout_days=holdout_days)

    if len(holdout) < _val.MIN_HOLDOUT_SAMPLES:
        return {"error": f"only {len(holdout)} holdout samples"}

    rewards = [r["reward_3d"] for r in holdout]

    results = {
        "generated_at":   datetime.utcnow().isoformat(),
        "holdout_days":   holdout_days,
        "holdout_size":   len(holdout),
        "reward_summary": {
            "mean": round(float(np.mean(rewards)), 4),
            "std":  round(float(np.std(rewards)), 4),
            "n_positive": int(sum(1 for r in rewards if r > 0)),
            "n_negative": int(sum(1 for r in rewards if r < 0)),
        },
        "methods": [],
    }

    # ── Method 1: XGBoost production (current rl_policy_model.pkl) ─
    prod_model = _rlpm._load_model()
    if prod_model is not None:
        actions = [_xgb_predict_action(prod_model, r)[0] for r in holdout]
        results["methods"].append(score_method(actions, rewards, "XGBoost (production)"))

    # ── Method 2: XGBoost shadow (if exists) ──────────────────────
    shadow_model = _rlpm._load_shadow()
    if shadow_model is not None:
        actions = [_xgb_predict_action(shadow_model, r)[0] for r in holdout]
        results["methods"].append(score_method(actions, rewards, "XGBoost (shadow)"))

    # ── Method 3: Production live system (Ollama+DeepSeek+XGB filter) ──
    actions = [_production_action(r)[0] for r in holdout]
    results["methods"].append(score_method(actions, rewards, "Production live (Ollama+DeepSeek)"))

    # ── Method 4: Trivial baselines ───────────────────────────────
    results["methods"].append(score_method(["BUY"]  * len(holdout), rewards, "Always BUY"))
    results["methods"].append(score_method(["HOLD"] * len(holdout), rewards, "Always HOLD"))

    # ── Method 5: LoRA adapter (slow — opt-in) ────────────────────
    if include_lora:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        adapter_dir = os.path.join(repo_root, "training", "lora_checkpoints", "best")
        if os.path.exists(os.path.join(adapter_dir, "adapter_model.safetensors")):
            try:
                import rl_lora_validator as _lv
                lora_metrics = _lv.validate_lora(adapter_dir, holdout, max_samples=max_lora_samples)
                if lora_metrics and "error" not in lora_metrics:
                    # Convert the LoRA validator output into the same unified shape
                    results["methods"].append({
                        "method":               "LoRA (Qwen3.5-35B-A3B + adapter)",
                        "total_signals":        lora_metrics.get("samples", 0),
                        "buy":                  None,
                        "sell":                 None,
                        "hold":                 lora_metrics.get("hold_count", 0),
                        "win_rate":             None,
                        "directional_accuracy": lora_metrics.get("directional_accuracy"),
                        "mean_realised_reward": lora_metrics.get("mean_realised_reward"),
                        "sharpe_proxy":         None,
                    })
                else:
                    results["lora_error"] = (lora_metrics or {}).get("error", "validation failed")
            except Exception as e:
                results["lora_error"] = str(e)
        else:
            results["lora_status"] = "no adapter yet — training still in progress"

    # ── Sort methods by directional_accuracy (desc) for quick read ─
    def _sort_key(m):
        return -1 if m.get("directional_accuracy") is None else -m["directional_accuracy"]
    results["methods"].sort(key=_sort_key)

    # ── Challenge set scoring (the real benchmark) ──────────────────
    if include_challenge_set:
        try:
            import rl_challenge_set as _cs
            challenge = _cs.load_challenge_set()
            if challenge:
                challenge_rewards = [r["reward_3d"] for r in challenge
                                      if isinstance(r.get("reward_3d"), (int, float))
                                      and math.isfinite(r["reward_3d"])]
                if challenge_rewards:
                    challenge_methods = []
                    if prod_model is not None:
                        actions = [_xgb_predict_action(prod_model, r)[0] for r in challenge]
                        challenge_methods.append(score_method(actions, challenge_rewards,
                                                              "XGBoost (production) — challenge set"))
                    if shadow_model is not None:
                        actions = [_xgb_predict_action(shadow_model, r)[0] for r in challenge]
                        challenge_methods.append(score_method(actions, challenge_rewards,
                                                              "XGBoost (shadow) — challenge set"))
                    actions = [_production_action(r)[0] for r in challenge]
                    challenge_methods.append(score_method(actions, challenge_rewards,
                                                          "Production live — challenge set"))
                    challenge_methods.sort(key=_sort_key)
                    results["challenge_set"] = {
                        "size":    len(challenge),
                        "methods": challenge_methods,
                    }
        except Exception as e:
            results["challenge_set_error"] = str(e)

    return results
