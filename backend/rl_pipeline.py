"""
RL Pipeline Orchestrator
========================
End-to-end MLOps cycle for AlphaTrader's RL models.  Runs daily as Task 19.

Cycle (one tick):
  1.  Back-fill rewards on the JSONL via update_trade_outcomes()
  2.  Refresh the catalyst/macro attribution report
  3.  Split records into train / holdout (last 7 days)
  4.  Train a candidate XGBoost model on train set
  5.  Score candidate AND current production model on holdout
  6.  Compare metrics  → decision: promote | shadow | reject
  7.  If "promote"   → overwrite rl_policy_model.pkl (hot-reloaded on next trade)
      If "shadow"    → save as rl_models/rl_policy_shadow.pkl (parallel logging)
      If "reject"    → keep production unchanged, record metrics for trend
  8.  Append run to rl_models/registry.json
  9.  Optionally trigger LoRA retraining if +5000 new labeled records

LoRA path: handled by trigger_lora_training_if_needed() — spawns the
training script as a subprocess (takes ~3 days).  Completion writes a
validation report to rl_models/lora_validation.json (no auto-deploy).
"""
import json
import logging
import os
import subprocess
import sys
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Threshold (in NEW labeled records since last LoRA training) that triggers
# a new LoRA training run.  Keeps training cost bounded while still
# incorporating fresh signal regularly.  At ~50-100 records/day this is
# roughly 20-40 days between LoRA trainings (each takes ~3 days to finish).
LORA_TRIGGER_THRESHOLD = 2000

# Lock file so the cycle never runs twice in parallel
_LOCK_FILE = "/tmp/rl_pipeline.lock"


def _acquire_lock() -> bool:
    """Single-writer lock so concurrent ticks don't trample each other."""
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)   # raises if pid not alive
            return False      # another instance is running
        except (OSError, ValueError):
            pass              # stale lock — claim it
    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock():
    if os.path.exists(_LOCK_FILE):
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass


# ──────────────────────────────────────────────
# Main cycle
# ──────────────────────────────────────────────

def run_cycle(holdout_days: int = 7) -> dict:
    """
    Execute one full pipeline cycle.  Returns a structured report.
    """
    import rl_data_collector as _rl
    import rl_policy_model    as _rlpm
    import rl_validation      as _val

    if not _acquire_lock():
        return {"status": "skipped", "reason": "another pipeline cycle running"}

    report = {
        "started_at":  datetime.utcnow().isoformat(),
        "status":      "running",
        "steps":       [],
        "decision":    None,
        "metrics":     {},
    }

    try:
        # ── Step 1: back-fill rewards ──────────────────────────
        try:
            _rl.update_trade_outcomes()
            report["steps"].append({"step": "backfill_outcomes", "ok": True})
        except Exception as e:
            logger.error(f"[Pipeline] backfill failed: {e}")
            report["steps"].append({"step": "backfill_outcomes", "ok": False, "error": str(e)})

        # ── Step 2: attribution report (best-effort) ───────────
        try:
            import intelligence_feedback as _ifb
            _ifb.run_attribution_analysis()
            report["steps"].append({"step": "attribution", "ok": True})
        except Exception as e:
            report["steps"].append({"step": "attribution", "ok": False, "error": str(e)})

        # ── Step 3: split train / holdout ──────────────────────
        records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
        train, holdout = _val.split_train_holdout(records, holdout_days=holdout_days)
        report["steps"].append({
            "step": "split",
            "ok": True,
            "train_n": len(train),
            "holdout_n": len(holdout),
        })

        if len(holdout) < _val.MIN_HOLDOUT_SAMPLES:
            report["status"] = "skipped"
            report["reason"] = f"only {len(holdout)} holdout samples (need {_val.MIN_HOLDOUT_SAMPLES})"
            return report

        # ── Step 4: train candidate ────────────────────────────
        candidate = _rlpm.train_model_on(train)
        if candidate is None:
            report["status"] = "skipped"
            report["reason"] = "training returned no model (insufficient data)"
            return report
        report["steps"].append({"step": "train_candidate", "ok": True, "train_n": len(train)})

        # ── Step 5: score candidate and production ─────────────
        candidate_metrics = _val.compute_xgb_metrics(candidate, holdout)
        prod_metrics      = None
        prod_model        = _rlpm._load_model()
        if prod_model is not None:
            prod_metrics = _val.compute_xgb_metrics(prod_model, holdout)
        baseline_metrics = _val.compute_baseline_metrics(holdout)
        report["metrics"] = {
            "candidate": candidate_metrics,
            "production": prod_metrics,
            "baseline":   baseline_metrics,
        }

        # ── Step 6: decision ───────────────────────────────────
        decision = _val.decide_promotion(candidate_metrics, prod_metrics)
        report["decision"] = decision
        logger.info(f"[Pipeline] Decision: {decision['decision']} — {decision['reason']}")

        # ── Step 7: act on decision ────────────────────────────
        version = datetime.utcnow().strftime("v%Y%m%d_%H%M%S")
        if decision["decision"] == "promote":
            _rlpm.save_versioned_model(candidate, version=version)
            _rlpm.remove_shadow()    # promoted overrides any pending shadow
            _val.register_version(version, "xgboost", candidate_metrics, decision,
                                  path=_rlpm.MODEL_FILE)
        elif decision["decision"] == "shadow":
            _rlpm.save_shadow_model(candidate, version=version)
            _val.register_version(version, "xgboost", candidate_metrics, decision,
                                  path=_rlpm.SHADOW_FILE)
        else:
            # reject — still register the run so trend metrics show negative results
            _val.register_version(version, "xgboost", candidate_metrics, decision,
                                  path="(rejected)")

        report["status"] = "ok"
        report["version"] = version

        # ── Step 8b: check if a previous LoRA training finished ─
        # If yes: validate → optionally deploy via vLLM hot-swap
        lora_completion = handle_lora_completion(records)
        if lora_completion:
            report["steps"].append({"step": "lora_completion", "ok": True, "result": lora_completion})

        # ── Step 9: LoRA trigger check ─────────────────────────
        lora_msg = trigger_lora_training_if_needed(records)
        if lora_msg:
            report["steps"].append({"step": "lora_trigger", "ok": True, "msg": lora_msg})

    except Exception as e:
        logger.error(f"[Pipeline] Unexpected error: {e}", exc_info=True)
        report["status"] = "error"
        report["error"]  = str(e)
    finally:
        _release_lock()

    report["finished_at"] = datetime.utcnow().isoformat()
    return report


# ──────────────────────────────────────────────
# LoRA completion handler (validate + deploy)
# ──────────────────────────────────────────────

_LORA_LAST_VALIDATED_FLAG = "rl_models/lora_last_validated_adapter.txt"


def handle_lora_completion(records: list) -> dict | None:
    """
    Check if a LoRA training subprocess has finished and produced an adapter
    in training/lora_checkpoints/best/.  If so:
      1. Skip if we already validated this exact adapter (idempotent)
      2. Validate on holdout via rl_lora_validator
      3. If approved (and auto-deploy is enabled), call rl_lora_deploy
      4. Always write the validation report to the registry

    Returns the result dict, or None if no new adapter to handle.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    adapter_dir = os.path.join(repo_root, "training", "lora_checkpoints", "best")
    if not os.path.exists(adapter_dir):
        return None

    # Use the adapter's mtime as a fingerprint — re-validate only on changes
    try:
        adapter_file = os.path.join(adapter_dir, "adapter_model.safetensors")
        if not os.path.exists(adapter_file):
            return None
        mtime = str(os.path.getmtime(adapter_file))
    except OSError:
        return None

    last_validated = ""
    flag_path = os.path.join(repo_root, _LORA_LAST_VALIDATED_FLAG)
    if os.path.exists(flag_path):
        with open(flag_path) as f:
            last_validated = f.read().strip()
    if last_validated == mtime:
        return None      # already handled this adapter

    # Check no LoRA training is currently running (don't validate a half-trained adapter)
    try:
        out = subprocess.check_output(["pgrep", "-f", "rl_lora_trainer"], text=True).strip()
        if out:
            logger.info("[LoRA Handle] training still in progress, skip validation")
            return None
    except subprocess.CalledProcessError:
        pass    # no training process — adapter is final

    import rl_validation as _val
    import rl_lora_validator as _lv

    # Build holdout from the same 7-day window
    _, holdout = _val.split_train_holdout(records, holdout_days=7)
    if len(holdout) < 30:
        return {"status": "skipped", "reason": "not enough holdout samples"}

    logger.info(f"[LoRA Handle] Validating adapter at {adapter_dir} (mtime={mtime})")
    metrics = _lv.validate_lora(adapter_dir, holdout, max_samples=100)
    prod_metrics = _lv.compare_to_production(metrics or {}, holdout)
    decision = _lv.decide_lora_promotion(metrics, prod_metrics)

    version = datetime.utcnow().strftime("v%Y%m%d_%H%M%S")
    _val.register_version(version, "lora",
                          metrics or {"error": "no metrics"},
                          decision, path=adapter_dir)

    # Mark this adapter as handled (don't re-validate on next cycle)
    os.makedirs(os.path.dirname(flag_path), exist_ok=True)
    with open(flag_path, "w") as f:
        f.write(mtime)

    # Auto-deploy if approved AND the safety toggle is enabled
    auto_deploy = _read_setting("lora_auto_deploy_enabled", "false") == "true"
    deployed = None
    if decision["decision"] == "deploy" and auto_deploy:
        logger.info(f"[LoRA Handle] Auto-deploying adapter (version {version})")
        import rl_lora_deploy as _dep
        deployed = _dep.deploy_adapter(adapter_dir, version=version)
    elif decision["decision"] == "deploy":
        logger.info("[LoRA Handle] Decision=deploy but auto-deploy disabled — manual promote via /api/rl/lora/deploy")

    return {
        "version":  version,
        "decision": decision,
        "metrics":  metrics,
        "deployed": deployed,
    }


def _read_setting(key: str, default: str) -> str:
    """Safe DB setting read with import-time-friendly fallback."""
    try:
        from database import get_db, get_setting
        db = next(get_db())
        try:
            return get_setting(db, key, 1, default)
        finally:
            db.close()
    except Exception:
        return default


# ──────────────────────────────────────────────
# LoRA auto-trigger
# ──────────────────────────────────────────────

def trigger_lora_training_if_needed(records: list) -> str | None:
    """
    Check if we've accumulated enough new labeled records since the last
    LoRA training run, and if so spawn the training script as a background
    subprocess.  Returns a status message or None if no action taken.
    """
    import rl_validation as _val
    registry = _val.load_registry()

    # Count current labeled records
    labeled = sum(1 for r in records
                  if isinstance(r.get("reward_3d"), (int, float))
                  and math.isfinite(r["reward_3d"]))

    # Get last LoRA training record count from registry
    lora_runs = [v for v in registry.get("versions", []) if v.get("kind") == "lora"]
    last_n    = lora_runs[-1].get("metrics", {}).get("samples", 0) if lora_runs else 0
    delta     = labeled - last_n

    if delta < LORA_TRIGGER_THRESHOLD:
        return f"LoRA not triggered: only +{delta} new labeled records (need {LORA_TRIGGER_THRESHOLD})"

    # Check if a LoRA training subprocess is already running
    try:
        out = subprocess.check_output(["pgrep", "-f", "rl_lora_trainer"], text=True).strip()
        if out:
            return f"LoRA already training (PID {out.splitlines()[0]})"
    except subprocess.CalledProcessError:
        pass    # no existing process — good to spawn

    # Spawn training subprocess
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    trainer   = os.path.join(repo_root, "training", "rl_lora_trainer.py")
    dataset   = os.path.join(repo_root, "training", "rl_sft_dataset")
    output    = os.path.join(repo_root, "training", "lora_checkpoints")
    py        = sys.executable

    if not os.path.exists(trainer):
        return f"LoRA trainer script not found: {trainer}"

    # Regenerate dataset first so it includes the new records
    prep_script = os.path.join(repo_root, "training", "prepare_rl_dataset.py")
    if os.path.exists(prep_script):
        try:
            subprocess.run(
                [py, prep_script, "--rl_data", _val.RL_DATA_FILE,
                 "--output", dataset],
                check=True, timeout=600,
                cwd=repo_root,
            )
        except Exception as e:
            return f"Dataset preparation failed: {e}"

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "1,2"
    env["PYTHONUNBUFFERED"]     = "1"
    log_path = "/tmp/rl_lora_auto.log"
    proc = subprocess.Popen(
        [py, "-u", trainer,
         "--dataset_dir", dataset,
         "--output_dir",  output,
         "--num_epochs", "1",
         "--per_device_batch_size", "1",
         "--gradient_accumulation_steps", "8",
         "--max_length", "2048",
         "--log_every_n_steps", "10",
         "--save_every_n_steps", "1000",
         "--max_gpus", "2"],
        stdout=open(log_path, "a"), stderr=subprocess.STDOUT,
        env=env, cwd=repo_root, start_new_session=True,
    )
    logger.info(f"[Pipeline] Spawned LoRA training subprocess PID {proc.pid}")
    return f"LoRA training spawned (PID {proc.pid}, log {log_path}) — {delta} new records"


# ──────────────────────────────────────────────
# Status / introspection
# ──────────────────────────────────────────────

def get_status() -> dict:
    """Return a snapshot of the pipeline state for the dashboard API."""
    import rl_validation as _val
    registry = _val.load_registry()

    # Find most recent run
    versions = registry.get("versions", [])
    last_run = versions[-1] if versions else None

    # Count decisions in last 30 days
    cutoff = datetime.utcnow() - timedelta(days=30)
    recent = []
    for v in versions:
        try:
            t = datetime.fromisoformat(v["created_at"])
            if t >= cutoff:
                recent.append(v)
        except Exception:
            continue
    decisions_30d = {"promote": 0, "shadow": 0, "reject": 0}
    for v in recent:
        d = v.get("decision", {}).get("decision", "")
        if d in decisions_30d:
            decisions_30d[d] += 1

    return {
        "production_version": registry.get("production"),
        "shadow_version":     registry.get("shadow"),
        "last_run":           last_run,
        "total_runs":         len(versions),
        "decisions_last_30d": decisions_30d,
        "recent_runs":        versions[-10:],   # last 10 for trend display
    }
