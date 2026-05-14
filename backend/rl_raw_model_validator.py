"""
Raw LLM Validator — Apples-to-Apples Model Comparison
=====================================================
Runs ANY trading-signal-generating LLM on the same challenge set and scores
it with the same unified decision rule, so we can compare:

  - Qwen2.5 (current Ollama)
  - Qwen3.5 base (no fine-tuning)
  - Qwen3.5 + LoRA adapter (after training)
  - Any other Ollama or HF model

Supported backends:
  - "ollama:{model_tag}"            → HTTP call to Ollama (fast, already running)
  - "vllm:{model_path}"             → spawn temp vLLM (heavy, base+adapter)
  - "hf:{model_id}[:adapter_path]"  → direct transformers load (slowest, but
                                       most flexible — used for Qwen3.5 base)

Workflow:
  1. Load challenge_test_set.jsonl (200 frozen hard examples) + the
     rolling holdout (last 7 days)
  2. For each record, build the same prompt the trainer used
  3. Call the LLM, parse BUY/SELL/HOLD from response
  4. Score via rl_compare.score_method() — same metric for every model
  5. Return structured report
"""
import json
import logging
import math
import os
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)

REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHALLENGE_FILE = os.path.join(REPO_ROOT, "rl_models", "challenge_test_set.jsonl")
RAW_REPORT_DIR = os.path.join(REPO_ROOT, "rl_models", "raw_model_reports")


# ──────────────────────────────────────────────
# Shared prompt format (matches rl_lora_validator)
# ──────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are AlphaTrader, an expert AI stock trading analyst. Given the market "
    "state and context, produce a structured trading signal: BUY, SELL, or HOLD. "
    "Start your response with exactly one line: 'Signal: BUY' / 'Signal: SELL' / "
    "'Signal: HOLD' — then give your reasoning."
)


def _build_user_prompt(rec: dict) -> str | None:
    state = rec.get("state") or {}
    indicators = state.get("indicators") or {}
    if not state.get("price"):
        return None
    return "\n".join([
        f"Symbol: {rec.get('symbol', 'UNKNOWN')}",
        f"Sector: {rec.get('sector', 'Other')}",
        f"Price: ${state.get('price')}",
        f"Day change: {state.get('change_pct', 'N/A')}%",
        f"PE: {state.get('pe_ratio', 'N/A')}",
        f"52w low/high: {state.get('fifty_two_week_low', 'N/A')} / {state.get('fifty_two_week_high', 'N/A')}",
        f"Valuation gap: {state.get('valuation_gap_pct', 'N/A')}%",
        f"VPA: {state.get('vpa_signal', 'N/A')}",
        f"RSI: {indicators.get('rsi', 'N/A')}",
        f"MACD: {indicators.get('macd', 'N/A')}",
        f"Event context: {rec.get('event_context_summary', '')[:300]}",
    ])


def _parse_action(text: str) -> str | None:
    """
    Extract a BUY/SELL/HOLD action from generated text.

    Priority:
      1. Explicit "Signal: BUY" / "Signal: SELL" pattern (most reliable)
      2. The LAST action keyword in the text (= the final decision after
         the model deliberates over options)

    Previous version (BUG): looped tokens in (BUY, SELL, HOLD) order and
    returned the first match found anywhere in the text — even if the
    model said "we could BUY but actually SELL" it returned BUY.  This
    biased reasoning-model evaluations toward BUY.
    """
    if not text:
        return None
    t = text.upper()

    # 1) Look for explicit "Signal: X" pattern first (strict, deterministic)
    m = re.search(r"SIGNAL[:\s]+(BUY|SELL|HOLD|SHORT|COVER)", t)
    if m:
        return m.group(1)

    # 2) Find ALL action-keyword positions, return the LAST one
    # (the model's final conclusion after weighing alternatives)
    matches = list(re.finditer(r"\b(BUY|SELL|HOLD|SHORT|COVER)\b", t))
    if matches:
        return matches[-1].group(1)
    return None


# ──────────────────────────────────────────────
# Backend dispatchers
# ──────────────────────────────────────────────

def _call_ollama(model_tag: str, system: str, user: str, host: str) -> str:
    """
    Call Ollama chat API.  Reasoning models (qwen3.5, deepseek-r1) emit a
    lot of "thinking" tokens before producing the actual answer — we give
    them a 600-token budget so the action verdict reliably comes through.
    """
    import requests
    # Reasoning models need more headroom for their internal thinking phase
    is_reasoning = any(tag in model_tag.lower() for tag in ("qwen3", "r1", "thinking"))
    num_predict = 600 if is_reasoning else 120

    payload = {
        "model": model_tag,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }
    r = requests.post(f"{host.rstrip('/')}/api/chat", json=payload, timeout=300)
    r.raise_for_status()
    msg = r.json()["message"]
    # Some reasoning models put the verdict in `thinking` if `content` truncates
    content = msg.get("content", "") or ""
    if not content.strip() and msg.get("thinking"):
        # Fallback: search the thinking trace for an action keyword
        content = msg["thinking"]
    return content


def _call_vllm(base_url: str, model_name: str, system: str, user: str) -> str:
    import requests
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 80,
    }
    r = requests.post(f"{base_url.rstrip('/')}/chat/completions",
                       json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


_hf_model_cache: dict = {}


def _call_hf(model_id: str, adapter_path: str | None,
             system: str, user: str,
             cuda_visible_devices: str = "1,2") -> str:
    """
    Direct transformers inference.  Heavy: takes ~10 min to load 35B model.
    Cached so we only pay the cost once per validator run.
    """
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", cuda_visible_devices)
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM

    key = f"{model_id}|{adapter_path or ''}"
    if key not in _hf_model_cache:
        logger.info(f"[Raw] Loading HF model {model_id} (adapter={adapter_path}) ...")
        tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        base = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=torch.bfloat16,
            trust_remote_code=True, device_map="auto",
        )
        base.eval()
        if adapter_path:
            from peft import PeftModel
            model = PeftModel.from_pretrained(base, adapter_path)
            model.eval()
        else:
            model = base
        _hf_model_cache[key] = (tok, model)
    tok, model = _hf_model_cache[key]
    first_device = next(model.parameters()).device

    prompt = f"<|system|>\n{system}\n<|user|>\n{user}\n<|assistant|>\n"
    enc = tok(prompt, return_tensors="pt", truncation=True,
              max_length=1800).to(first_device)
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=80, do_sample=False,
                              pad_token_id=tok.pad_token_id)
    return tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)


# ──────────────────────────────────────────────
# Main validation entry point
# ──────────────────────────────────────────────

def validate_model(
    backend_spec: str,
    records: list,
    max_samples: int = 100,
    ollama_host: str = "http://localhost:11434",
) -> dict:
    """
    backend_spec examples:
      "ollama:qwen2.5-coder:32b"
      "ollama:qwen3.5:35b"
      "vllm:http://127.0.0.1:11500/v1|alphatrader-lora"
      "hf:Qwen/Qwen3.5-35B-A3B"
      "hf:Qwen/Qwen3.5-35B-A3B|training/lora_checkpoints/best"
    """
    # Filter to usable records
    usable = []
    for r in records:
        reward = r.get("reward_3d")
        if not isinstance(reward, (int, float)) or not math.isfinite(reward):
            continue
        if abs(reward) < 1.0:
            continue
        if _build_user_prompt(r) is None:
            continue
        usable.append(r)
    if not usable:
        return {"error": "no usable records"}
    usable = usable[:max_samples]
    logger.info(f"[Raw] Scoring {len(usable)} records with {backend_spec}")

    started = time.time()
    actions, rewards = [], []
    errors = 0

    for i, rec in enumerate(usable):
        user_msg = _build_user_prompt(rec)
        try:
            if backend_spec.startswith("ollama:"):
                tag = backend_spec[len("ollama:"):]
                response = _call_ollama(tag, SYSTEM_PROMPT, user_msg, ollama_host)
            elif backend_spec.startswith("vllm:"):
                rest = backend_spec[len("vllm:"):]
                base_url, model_name = rest.split("|", 1)
                response = _call_vllm(base_url, model_name, SYSTEM_PROMPT, user_msg)
            elif backend_spec.startswith("hf:"):
                rest = backend_spec[len("hf:"):]
                if "|" in rest:
                    model_id, adapter = rest.split("|", 1)
                else:
                    model_id, adapter = rest, None
                response = _call_hf(model_id, adapter, SYSTEM_PROMPT, user_msg)
            else:
                return {"error": f"unknown backend: {backend_spec}"}
        except Exception as e:
            logger.warning(f"[Raw] call failed on {i}: {e}")
            errors += 1
            actions.append("SKIP")
            rewards.append(rec["reward_3d"])
            continue

        action = _parse_action(response) or "SKIP"
        actions.append(action)
        rewards.append(rec["reward_3d"])

        if (i + 1) % 20 == 0:
            elapsed = time.time() - started
            logger.info(f"[Raw] {i+1}/{len(usable)}  ({elapsed:.0f}s elapsed, ~{elapsed/(i+1):.1f}s/sample)")

    # Unified scoring (same as rl_compare.score_method)
    import rl_compare as _cmp
    metrics = _cmp.score_method(actions, rewards, backend_spec)
    metrics["elapsed_seconds"] = round(time.time() - started, 1)
    metrics["errors"]          = errors
    return metrics


def run_baseline_shootout(test_set: str = "challenge",
                          max_samples: int = 100) -> dict:
    """
    Run every available backend on the same test set and produce a single
    head-to-head report.  Skips backends that are not reachable.

    test_set:
      "challenge"  → challenge_test_set.jsonl (200 frozen hard examples)
      "holdout"    → rolling last-7-day holdout
      "combined"   → challenge ∪ holdout (deduped)
    """
    import rl_data_collector as _rl
    import rl_validation      as _val
    import rl_challenge_set   as _cs

    # Assemble records to test on
    if test_set == "challenge":
        records = _cs.load_challenge_set()
    elif test_set == "holdout":
        all_records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
        _, records = _val.split_train_holdout(all_records, holdout_days=7)
    elif test_set == "combined":
        all_records = _rl._parse_jsonl(_rl.RL_DATA_FILE)
        _, holdout = _val.split_train_holdout(all_records, holdout_days=7)
        challenge  = _cs.load_challenge_set()
        seen = set()
        records = []
        for r in holdout + challenge:
            fp = f"{r.get('timestamp')}|{r.get('symbol')}"
            if fp in seen:
                continue
            seen.add(fp)
            records.append(r)
    else:
        return {"error": f"unknown test_set: {test_set}"}

    if not records:
        return {"error": "test set is empty"}

    # Auto-detect which backends are reachable
    backends = []
    import requests
    # Probe Ollama (default + alt port)
    for port in (11434, 11435):
        try:
            r = requests.get(f"http://localhost:{port}/api/tags", timeout=2)
            if r.ok:
                tags = [m["name"] for m in r.json().get("models", [])]
                for tag in tags:
                    if "qwen" in tag.lower() or "llama" in tag.lower():
                        backends.append((f"ollama:{tag}", f"http://localhost:{port}"))
        except Exception:
            continue

    # Probe LoRA vLLM if up
    try:
        r = requests.get("http://127.0.0.1:11500/v1/models", timeout=2)
        if r.ok:
            backends.append(("vllm:http://127.0.0.1:11500/v1|alphatrader-lora", None))
    except Exception:
        pass

    # Run each backend
    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "test_set":     test_set,
        "test_set_size": len(records),
        "results":      [],
    }
    for spec, host in backends:
        try:
            kwargs = {"max_samples": max_samples}
            if host:
                kwargs["ollama_host"] = host
            metrics = validate_model(spec, records, **kwargs)
            report["results"].append(metrics)
        except Exception as e:
            logger.error(f"[Raw] {spec} failed: {e}")
            report["results"].append({"method": spec, "error": str(e)})

    # Sort by directional_accuracy desc
    def _key(m):
        v = m.get("directional_accuracy")
        return -1 if v is None else -v
    report["results"].sort(key=_key)

    # Persist for later inspection
    os.makedirs(RAW_REPORT_DIR, exist_ok=True)
    fname = f"shootout_{test_set}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    with open(os.path.join(RAW_REPORT_DIR, fname), "w") as f:
        json.dump(report, f, indent=2, default=str)
    report["report_file"] = fname
    return report
