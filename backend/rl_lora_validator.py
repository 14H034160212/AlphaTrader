"""
LoRA Adapter Validator
======================
After rl_lora_trainer.py finishes, the pipeline calls validate_lora() to
score the new adapter against holdout records (same 7-day holdout used
for XGBoost validation).

Approach:
  1. Load base model (Qwen3.5-35B-A3B) + adapter via peft
  2. For each of N holdout records, build the same prompt format the
     trainer saw, generate a short response, parse out the predicted
     BUY/SELL/HOLD action
  3. Compare predicted action against the actual reward_3d:
       - BUY  + reward_3d > 0  → correct
       - SELL + reward_3d < 0  → correct
       - HOLD                  → graded by reward magnitude
  4. Compute directional accuracy, mean realised reward (if signals
     had been followed)
  5. Compare against the production baseline (current Ollama
     deepseek_ai output already logged in JSONL)

Resource: ~70 GB VRAM split across the 2 GPUs reserved for training.
Validation on 100 holdout samples ≈ 8-15 minutes.
"""
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _parse_action(text: str) -> str | None:
    """Pull a BUY/SELL/HOLD signal out of generated text."""
    if not text:
        return None
    t = text.upper()
    # Look for "Signal: BUY" pattern first (matches our training format)
    m = re.search(r"SIGNAL[:\s]+(BUY|SELL|HOLD|SHORT|COVER)", t)
    if m:
        return m.group(1)
    # Fallback: scan for any standalone token
    for tok in ("BUY", "SELL", "HOLD"):
        if re.search(rf"\b{tok}\b", t):
            return tok
    return None


def _format_prompt(rec: dict) -> str | None:
    """
    Recreate the same prompt format the trainer used.  Returns None if
    record is incomplete.
    """
    state = rec.get("state") or {}
    indicators = state.get("indicators") or {}
    if not state.get("price"):
        return None

    system = (
        "You are AlphaTrader, an expert AI stock trading analyst. "
        "Given the market state and context, produce a structured trading signal: "
        "BUY / SELL / HOLD with confidence (0-1), target price, stop loss, "
        "recommended portfolio weight %, time horizon, and detailed reasoning. "
        "Prioritize large-cap, well-known companies."
    )
    lines = [
        f"Symbol: {rec.get('symbol', 'UNKNOWN')}",
        f"Sector: {rec.get('sector', 'Other')}",
        f"Price: ${state.get('price')}",
        f"Day change: {state.get('change_pct', 'N/A')}%",
        f"PE ratio: {state.get('pe_ratio', 'N/A')}",
        f"52w low/high: {state.get('fifty_two_week_low', 'N/A')} / {state.get('fifty_two_week_high', 'N/A')}",
        f"Valuation gap: {state.get('valuation_gap_pct', 'N/A')}%",
        f"VPA signal: {state.get('vpa_signal', 'N/A')}",
        f"RSI: {indicators.get('rsi', 'N/A')}",
        f"MACD: {indicators.get('macd', 'N/A')}",
        f"Event context: {rec.get('event_context_summary', '')}",
    ]
    return f"<|system|>\n{system}\n<|user|>\n" + "\n".join(lines) + "\n<|assistant|>\n"


def validate_lora(
    adapter_path: str,
    holdout_records: list,
    base_model: str = "Qwen/Qwen3.5-35B-A3B",
    max_samples: int = 100,
    cuda_visible_devices: str = "1,2",
) -> dict | None:
    """
    Run inference on holdout records with base + adapter, compute metrics.
    Returns None on failure.
    """
    # Filter to records with realised reward + non-trivial magnitude
    usable = []
    for r in holdout_records:
        reward = r.get("reward_3d")
        if not isinstance(reward, (int, float)) or not math.isfinite(reward):
            continue
        if abs(reward) < 0.5:    # near-zero noise
            continue
        if _format_prompt(r) is None:
            continue
        usable.append(r)

    if len(usable) < 30:
        return {"error": f"only {len(usable)} usable holdout samples"}

    usable = usable[:max_samples]
    logger.info(f"[LoRA Validate] Scoring {len(usable)} holdout records...")

    # Force GPU placement BEFORE importing torch
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", cuda_visible_devices)

    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import PeftModel

    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    logger.info("[LoRA Validate] Loading base model (1-2 min)...")
    base = AutoModelForCausalLM.from_pretrained(
        base_model,
        dtype=torch.bfloat16,
        trust_remote_code=True,
        device_map="auto",
    )
    base.eval()

    logger.info(f"[LoRA Validate] Loading adapter from {adapter_path}")
    model = PeftModel.from_pretrained(base, adapter_path)
    model.eval()

    first_device = next(model.parameters()).device

    # Run inference
    correct = 0
    realised_reward_sum = 0.0
    hold_count = 0
    invalid = 0
    for i, rec in enumerate(usable):
        prompt = _format_prompt(rec)
        enc = tokenizer(prompt, return_tensors="pt", truncation=True,
                        max_length=1800).to(first_device)
        try:
            with torch.no_grad():
                out = model.generate(
                    **enc,
                    max_new_tokens=80,
                    do_sample=False,
                    pad_token_id=tokenizer.pad_token_id,
                )
            generated = tokenizer.decode(out[0][enc["input_ids"].shape[1]:],
                                          skip_special_tokens=True)
        except Exception as e:
            invalid += 1
            logger.debug(f"[LoRA Validate] gen failed on {i}: {e}")
            continue

        predicted = _parse_action(generated)
        actual_reward = rec["reward_3d"]
        if predicted is None:
            invalid += 1
            continue

        if predicted == "HOLD":
            hold_count += 1
            # HOLD on a near-zero outcome = correct, otherwise miss
            if abs(actual_reward) < 1.0:
                correct += 1
        elif predicted in ("BUY", "COVER"):
            if actual_reward > 0:
                correct += 1
                realised_reward_sum += actual_reward
            else:
                realised_reward_sum += actual_reward     # took the loss
        elif predicted in ("SELL", "SHORT"):
            if actual_reward < 0:
                correct += 1
                realised_reward_sum += -actual_reward
            else:
                realised_reward_sum += -actual_reward

        if (i + 1) % 25 == 0:
            logger.info(f"[LoRA Validate] {i+1}/{len(usable)} processed")

    n_acted = len(usable) - invalid - hold_count
    return {
        "samples":              len(usable),
        "invalid":              invalid,
        "hold_count":           hold_count,
        "correct":              correct,
        "directional_accuracy": round(correct / max(1, len(usable) - invalid) * 100, 2),
        "mean_realised_reward": round(realised_reward_sum / max(1, n_acted), 4) if n_acted else 0,
        "adapter_path":         adapter_path,
        "validated_at":         datetime.utcnow().isoformat(),
    }


def compare_to_production(lora_metrics: dict, holdout_records: list) -> dict:
    """
    Build the production baseline by looking at what the LIVE DeepSeek+
    XGBoost stack actually decided on these same holdout records (stored
    as 'action' and 'rl_policy_score' in JSONL).
    """
    correct = 0
    realised_reward_sum = 0.0
    n_acted = 0
    for r in holdout_records:
        reward = r.get("reward_3d")
        if not isinstance(reward, (int, float)) or not math.isfinite(reward) or abs(reward) < 0.5:
            continue
        action = r.get("action")
        if action == "HOLD":
            if abs(reward) < 1.0:
                correct += 1
            n_acted += 0   # HOLD didn't act
        elif action in ("BUY", "COVER"):
            n_acted += 1
            realised_reward_sum += reward
            if reward > 0:
                correct += 1
        elif action in ("SELL", "SHORT"):
            n_acted += 1
            realised_reward_sum += -reward
            if reward < 0:
                correct += 1

    return {
        "samples":              n_acted,
        "directional_accuracy": round(correct / max(1, n_acted) * 100, 2) if n_acted else None,
        "mean_realised_reward": round(realised_reward_sum / max(1, n_acted), 4) if n_acted else None,
    }


def decide_lora_promotion(lora_metrics: dict, prod_metrics: dict) -> dict:
    """
    LoRA promotion criteria (more conservative than XGBoost since deploying
    a 35B model is heavy):
      - dir_acc improvement >= 5pp AND realised reward improvement > 0 → deploy
      - dir_acc improvement in (0, 5pp)                                → shadow
      - worse                                                          → reject
    """
    if lora_metrics is None or "error" in lora_metrics:
        return {"decision": "reject", "reason": "validation failed",
                "delta": {}, "lora_metrics": lora_metrics}

    if prod_metrics is None or prod_metrics.get("directional_accuracy") is None:
        return {"decision": "deploy",
                "reason": "no production baseline — first deployment",
                "delta": {}, "lora_metrics": lora_metrics}

    delta_acc = lora_metrics["directional_accuracy"] - prod_metrics["directional_accuracy"]
    delta_reward = (lora_metrics["mean_realised_reward"]
                    - (prod_metrics.get("mean_realised_reward") or 0))
    delta = {"dir_acc_pp": round(delta_acc, 2),
             "mean_reward": round(delta_reward, 4)}

    if delta_acc >= 5.0 and delta_reward > 0:
        return {"decision": "deploy",
                "reason": f"+{delta_acc:.1f}pp dir-acc, +{delta_reward:.2f}% reward",
                "delta": delta, "lora_metrics": lora_metrics, "prod_metrics": prod_metrics}
    if delta_acc > 0:
        return {"decision": "shadow",
                "reason": f"+{delta_acc:.1f}pp dir-acc — running shadow",
                "delta": delta, "lora_metrics": lora_metrics, "prod_metrics": prod_metrics}
    return {"decision": "reject",
            "reason": f"{delta_acc:+.1f}pp dir-acc — worse than production",
            "delta": delta, "lora_metrics": lora_metrics, "prod_metrics": prod_metrics}
