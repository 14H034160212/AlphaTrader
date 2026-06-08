"""
One-shot LoRA validation runner.
Evaluates training/lora_checkpoints/best/ against the permanent challenge_test_set.jsonl
holdout, then compares against the production XGBoost baseline currently in
rl_models/registry.json.
Does NOT change production.
"""
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lora_val")

ROOT        = os.path.dirname(os.path.abspath(__file__))
ADAPTER     = os.path.join(ROOT, "training", "lora_checkpoints", "best")
HOLDOUT     = os.path.join(ROOT, "rl_models", "challenge_test_set.jsonl")
REGISTRY    = os.path.join(ROOT, "rl_models", "registry.json")
BASE_MODEL  = "Qwen/Qwen3.5-35B-A3B"

sys.path.insert(0, os.path.join(ROOT, "backend"))
from rl_lora_validator import validate_lora, compare_to_production, decide_lora_promotion


def load_holdout(path: str) -> list:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def main():
    log.info(f"Adapter:  {ADAPTER}")
    log.info(f"Holdout:  {HOLDOUT}")

    holdout = load_holdout(HOLDOUT)
    log.info(f"Loaded {len(holdout)} holdout records")

    lora_metrics = validate_lora(
        adapter_path=ADAPTER,
        holdout_records=holdout,
        base_model=BASE_MODEL,
        max_samples=100,
        cuda_visible_devices="0,1",
    )
    log.info(f"LoRA metrics: {json.dumps(lora_metrics, indent=2)}")

    prod_metrics = compare_to_production(lora_metrics or {}, holdout)
    log.info(f"Prod (DeepSeek live logged) baseline: {json.dumps(prod_metrics, indent=2)}")

    if os.path.exists(REGISTRY):
        with open(REGISTRY) as f:
            reg = json.load(f)
        log.info(f"XGB registry current: {reg.get('production')}")

    decision = decide_lora_promotion(lora_metrics, prod_metrics)
    log.info(f"DECISION: {json.dumps(decision, indent=2)}")

    out_path = os.path.join(ROOT, "rl_models", "raw_model_reports", "lora_validation_latest.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"lora": lora_metrics, "prod": prod_metrics, "decision": decision}, f, indent=2)
    log.info(f"Wrote report → {out_path}")


if __name__ == "__main__":
    main()
