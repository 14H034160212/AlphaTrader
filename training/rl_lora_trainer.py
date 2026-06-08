"""
SerenityTrader RL LoRA Trainer (Path 2)
=====================================
Fine-tunes Qwen3.5-35B-A3B (MoE) with LoRA adapters using reward-weighted SFT
on SerenityTrader's real trading P&L data.

Model: Qwen/Qwen3.5-35B-A3B
  - MoE architecture: 35B total params, only ~3B active per token (8 routed + 1 shared expert)
  - bfloat16 size ≈ 70GB → split across 2 A100 80GB via device_map="auto" (~35GB each)
  - LoRA applied to attention modules only (q/k/v/o_proj) — skips routed experts to
    avoid 256x parameter explosion in the MoE FFN layers

Strategy: Reward-weighted SFT (offline RL)
  - Records where reward_3d > 0 (correct signals) → high loss weight
  - Records where reward_3d < 0 (wrong signals)   → near-zero loss weight
  - Result: model learns to produce signals that actually made money

Why reward-weighted SFT instead of GRPO/PPO first:
  - 39k labeled records already exist — no need for online rollouts
  - Stable training, no reward hacking risk
  - Can upgrade to GRPO after validating the adapter

Hardware (max 2 GPUs, auto-selected):
  - Auto-selects GPUs with most free VRAM (currently GPU 1 + GPU 2, each 81GB free)
  - Single process + device_map="auto" — do NOT use torchrun

First-time setup (download model, ~70GB):
  python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3.5-35B-A3B')"

Usage:
  # Step 1: prepare dataset (run once)
  python prepare_rl_dataset.py --rl_data ../rl_training_data.jsonl --output ./rl_sft_dataset

  # Step 2: train
  python rl_lora_trainer.py \
      --dataset_dir ./rl_sft_dataset \
      --output_dir ./lora_checkpoints

  # Override GPU selection manually:
  CUDA_VISIBLE_DEVICES=1,2 python rl_lora_trainer.py ...

  # Step 3: deploy adapter
  # Load via: peft.PeftModel.from_pretrained(base_model, './lora_checkpoints/best')
"""

import argparse
import json
import math
import os
import subprocess
import sys

import torch
from torch.utils.data import Dataset, DataLoader


# ──────────────────────────────────────────────
# GPU Auto-selection
# ──────────────────────────────────────────────

def select_free_gpus(max_gpus: int = 2, min_free_gb: float = 40.0) -> list[int]:
    """
    Query nvidia-smi for free VRAM and return up to max_gpus GPU indices,
    sorted by most free memory first.  Skips GPUs with < min_free_gb free.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=index,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            text=True
        ).strip()
    except FileNotFoundError:
        print("[GPU] nvidia-smi not found — defaulting to cuda:0")
        return [0]

    candidates = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        idx      = int(parts[0])
        free_mb  = int(parts[1])
        util_pct = int(parts[2])
        free_gb  = free_mb / 1024
        if free_gb >= min_free_gb:
            candidates.append((idx, free_gb, util_pct))
            print(f"  GPU {idx}: {free_gb:.1f} GB free, {util_pct}% utilization")
        else:
            print(f"  GPU {idx}: {free_gb:.1f} GB free — SKIPPED (< {min_free_gb:.0f} GB)")

    # Sort by free memory descending, then pick top max_gpus
    candidates.sort(key=lambda x: -x[1])
    selected = [c[0] for c in candidates[:max_gpus]]
    print(f"\n[GPU] Selected: {selected} ({len(selected)}/{max_gpus} GPUs)")
    return selected


# ──────────────────────────────────────────────
# Dataset
# ──────────────────────────────────────────────

class RLSFTDataset(Dataset):
    def __init__(self, jsonl_path: str, tokenizer, max_length: int = 2048):
        self.samples = []
        self.tokenizer = tokenizer
        self.max_length = max_length

        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.samples.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        print(f"  Loaded {len(self.samples)} samples from {jsonl_path}")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        row = self.samples[idx]
        prompt   = row["prompt"]
        response = row["response"]
        weight   = float(row.get("weight", 1.0))

        # Tokenize prompt and response SEPARATELY so we can left-truncate the
        # prompt while preserving the full response.  The previous version
        # right-truncated the concatenation, which silently chopped off the
        # response (median prompt ≈ 1963 tokens, max_length 1024-2048) →
        # all labels became -100 and loss was always 0.
        prompt_ids   = self.tokenizer(f"{prompt}\n<|assistant|>\n",
                                       add_special_tokens=True)["input_ids"]
        response_ids = self.tokenizer(response,
                                       add_special_tokens=False)["input_ids"]
        eos = self.tokenizer.eos_token_id
        if eos is not None and (not response_ids or response_ids[-1] != eos):
            response_ids = response_ids + [eos]

        # Reserve space for response (cap at 256 — responses are ~150 tokens median)
        resp_max = min(256, self.max_length // 4)
        if len(response_ids) > resp_max:
            response_ids = response_ids[:resp_max]

        # Left-truncate prompt so prompt + response fits in max_length
        budget = self.max_length - len(response_ids)
        if len(prompt_ids) > budget:
            prompt_ids = prompt_ids[-budget:]   # keep the most recent context

        seq = prompt_ids + response_ids
        prompt_len = len(prompt_ids)

        # Pad to max_length
        pad_id  = self.tokenizer.pad_token_id or 0
        pad_len = self.max_length - len(seq)
        input_ids      = torch.tensor(seq + [pad_id] * pad_len, dtype=torch.long)
        attention_mask = torch.tensor([1] * len(seq) + [0] * pad_len, dtype=torch.long)

        # Mask out prompt + padding from loss; only response tokens contribute
        labels = input_ids.clone()
        labels[:prompt_len] = -100
        labels[attention_mask == 0] = -100

        return {
            "input_ids":      input_ids,
            "attention_mask": attention_mask,
            "labels":         labels,
            "weight":         torch.tensor(weight, dtype=torch.float32),
        }


# ──────────────────────────────────────────────
# Custom weighted loss
# ──────────────────────────────────────────────

def weighted_cross_entropy(logits, labels, weights):
    """Per-sample reward-weighted cross-entropy loss."""
    loss_fn = torch.nn.CrossEntropyLoss(reduction="none", ignore_index=-100)
    B, T, V = logits.shape
    shift_logits = logits[..., :-1, :].contiguous().view(-1, V)
    shift_labels = labels[..., 1:].contiguous().view(-1)

    token_loss  = loss_fn(shift_logits, shift_labels).view(B, T - 1)
    valid_mask  = (labels[..., 1:] != -100).float()
    sample_loss = (token_loss * valid_mask).sum(dim=1) / valid_mask.sum(dim=1).clamp(min=1)

    w = weights.to(sample_loss.device)
    return (sample_loss * w).mean()


# ──────────────────────────────────────────────
# Main training loop
# ──────────────────────────────────────────────

def train(args):
    from transformers import AutoTokenizer, AutoModelForCausalLM, get_linear_schedule_with_warmup
    from peft import LoraConfig, get_peft_model, TaskType

    # ── GPU info ───────────────────────────────────────────
    # CUDA_VISIBLE_DEVICES was already set before this process started
    # (via os.execvpe in __main__ or via user-supplied env var).
    # By the time train() is called, PyTorch can only see the selected GPUs
    # remapped as logical 0, 1, ...
    num_gpus = torch.cuda.device_count()
    cv = os.environ.get("CUDA_VISIBLE_DEVICES", "all")
    print(f"[GPU] Training on {num_gpus} GPU(s) (CUDA_VISIBLE_DEVICES={cv})", flush=True)

    # For MoE models (Qwen3.5), even QLoRA can't fit on 1 GPU because the
    # 256 expert layers aren't all quantized → still need pipeline parallel.
    # Dense models in QLoRA mode CAN fit on 1 GPU.
    is_moe_model = "moe" in args.model_name_or_path.lower() or "a3b" in args.model_name_or_path.lower()
    if args.use_qlora and not is_moe_model and num_gpus == 1:
        device_map = {"": 0}
        print("[GPU] QLoRA + dense + single GPU: placing on cuda:0", flush=True)
    else:
        device_map = "auto" if num_gpus > 1 else {"": 0}
        print(f"[GPU] Using device_map={device_map}", flush=True)

    # ── Tokenizer ──────────────────────────────────────────
    print(f"\n[Trainer] Loading tokenizer from {args.model_name_or_path}")
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        trust_remote_code=True,
        padding_side="right",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ── Model ──────────────────────────────────────────────
    print(f"[Trainer] Loading model with device_map={device_map!r} ...")
    load_kwargs = dict(
        dtype=torch.bfloat16,
        trust_remote_code=True,
        device_map=device_map,
    )
    if args.use_qlora:
        from transformers import BitsAndBytesConfig
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        print("[Trainer] QLoRA mode: 4-bit quantization enabled")

    model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, **load_kwargs)
    model.gradient_checkpointing_enable()
    if args.use_qlora:
        # For MoE models, the standard prepare_model_for_kbit_training casts
        # all non-quantized params (including bf16 expert layers) to float32,
        # doubling memory.  Manually do the minimal prep instead: just enable
        # input gradients so gradient checkpointing works with frozen base.
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        else:
            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)
            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

    # Print per-GPU memory after loading (logical GPU indices after CUDA_VISIBLE_DEVICES)
    cv_list = [g.strip() for g in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if g.strip()]
    for i in range(num_gpus):
        alloc = torch.cuda.memory_allocated(i) / 1e9
        total = torch.cuda.get_device_properties(i).total_memory / 1e9
        phys  = cv_list[i] if i < len(cv_list) else str(i)
        print(f"  GPU {phys} (logical {i}): {alloc:.1f} / {total:.1f} GB used after model load")

    # ── LoRA ───────────────────────────────────────────────
    # For MoE models (qwen35moe): apply LoRA to attention only.
    # Skipping gate/up/down projections avoids 256x parameter explosion
    # from the routed expert FFN layers — each layer has 256 expert copies.
    is_moe = "moe" in args.model_name_or_path.lower() or "a3b" in args.model_name_or_path.lower()
    if is_moe:
        lora_target_modules = ["q_proj", "k_proj", "v_proj", "o_proj"]
        print(f"[LoRA] MoE model detected — targeting attention modules only: {lora_target_modules}")
    else:
        lora_target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                               "gate_proj", "up_proj", "down_proj"]
        print(f"[LoRA] Dense model — targeting all linear modules: {lora_target_modules}")

    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        target_modules=lora_target_modules,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── Dataset ────────────────────────────────────────────
    train_ds = RLSFTDataset(
        os.path.join(args.dataset_dir, "train.jsonl"), tokenizer, args.max_length)
    val_ds   = RLSFTDataset(
        os.path.join(args.dataset_dir, "val.jsonl"),   tokenizer, args.max_length)

    train_loader = DataLoader(
        train_ds, batch_size=args.per_device_batch_size,
        shuffle=True, num_workers=2, pin_memory=False,
    )
    val_loader = DataLoader(
        val_ds, batch_size=args.per_device_batch_size, num_workers=2)

    # ── Optimizer & Scheduler ──────────────────────────────
    optimizer = torch.optim.AdamW(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.learning_rate, weight_decay=0.01,
    )
    total_steps  = len(train_loader) * args.num_epochs // args.gradient_accumulation_steps
    warmup_steps = max(1, total_steps // 10)
    scheduler    = get_linear_schedule_with_warmup(optimizer, warmup_steps, total_steps)

    os.makedirs(args.output_dir, exist_ok=True)
    best_val_loss = float("inf")

    # With device_map="auto", inputs go to the first device; model handles the rest
    first_device = next(model.parameters()).device

    import time
    for epoch in range(args.num_epochs):
        model.train()
        total_loss = 0.0
        optimizer.zero_grad()
        epoch_start = time.time()
        last_log_t  = time.time()

        for step, batch in enumerate(train_loader):
            input_ids      = batch["input_ids"].to(first_device)
            attention_mask = batch["attention_mask"].to(first_device)
            labels         = batch["labels"].to(first_device)
            weights        = batch["weight"].to(first_device)

            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            loss    = weighted_cross_entropy(outputs.logits, labels, weights)
            loss    = loss / args.gradient_accumulation_steps
            loss.backward()
            total_loss += loss.item() * args.gradient_accumulation_steps

            # Per-step heartbeat so we see speed immediately
            if step < 5 or (step + 1) % args.log_every_n_steps == 0:
                dt = time.time() - last_log_t
                last_log_t = time.time()
                print(f"  E{epoch+1} step {step+1}/{len(train_loader)}  "
                      f"loss={loss.item()*args.gradient_accumulation_steps:.4f}  "
                      f"dt={dt:.1f}s", flush=True)

            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(
                    filter(lambda p: p.requires_grad, model.parameters()), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

            # Mid-epoch checkpoint so partial training is usable
            if args.save_every_n_steps > 0 and (step + 1) % args.save_every_n_steps == 0:
                ckpt = os.path.join(args.output_dir, f"step_{step+1}")
                model.save_pretrained(ckpt)
                print(f"  [Checkpoint] step {step+1} → {ckpt}", flush=True)

        # Validation
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch in val_loader:
                input_ids      = batch["input_ids"].to(first_device)
                attention_mask = batch["attention_mask"].to(first_device)
                labels         = batch["labels"].to(first_device)
                weights        = batch["weight"].to(first_device)
                outputs        = model(input_ids=input_ids, attention_mask=attention_mask)
                val_loss      += weighted_cross_entropy(outputs.logits, labels, weights).item()
        val_loss /= max(1, len(val_loader))
        print(f"\nEpoch {epoch+1} complete — "
              f"train_loss={total_loss/len(train_loader):.4f}  "
              f"val_loss={val_loss:.4f}")

        # Save checkpoint
        ckpt_dir = os.path.join(args.output_dir, f"epoch_{epoch+1}")
        model.save_pretrained(ckpt_dir)
        tokenizer.save_pretrained(ckpt_dir)
        print(f"  Checkpoint saved → {ckpt_dir}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_dir = os.path.join(args.output_dir, "best")
            model.save_pretrained(best_dir)
            tokenizer.save_pretrained(best_dir)
            print(f"  New best val_loss={val_loss:.4f} → {best_dir}")

    print(f"\n[Trainer] Done. Best val_loss={best_val_loss:.4f}")
    print(f"  Best adapter: {os.path.join(args.output_dir, 'best')}")
    print("\nDeploy:")
    print("  from peft import PeftModel")
    print(f"  model = PeftModel.from_pretrained(base_model, '{os.path.join(args.output_dir, 'best')}')")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SerenityTrader RL LoRA Trainer")
    parser.add_argument("--model_name_or_path",  default="Qwen/Qwen3.5-35B-A3B",
                        help="HuggingFace model ID (default: Qwen/Qwen3.5-35B-A3B)")
    parser.add_argument("--dataset_dir",         default="./rl_sft_dataset")
    parser.add_argument("--output_dir",          default="./lora_checkpoints")
    parser.add_argument("--max_gpus",            type=int,   default=2,
                        help="Maximum GPUs to use (default: 2)")
    parser.add_argument("--num_epochs",          type=int,   default=3)
    parser.add_argument("--per_device_batch_size", type=int, default=1)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8)
    parser.add_argument("--learning_rate",       type=float, default=2e-4)
    parser.add_argument("--max_length",          type=int,   default=2048)
    parser.add_argument("--lora_r",              type=int,   default=16)
    parser.add_argument("--lora_alpha",          type=int,   default=32)
    parser.add_argument("--use_qlora",           action="store_true",
                        help="Use 4-bit QLoRA to halve VRAM usage (optional)")
    parser.add_argument("--log_every_n_steps",   type=int,   default=5,
                        help="How often to print loss (default: every 5 steps)")
    parser.add_argument("--save_every_n_steps",  type=int,   default=1000,
                        help="Save mid-epoch checkpoint every N steps (0=off)")
    args = parser.parse_args()

    # Safety cap — never exceed 2 GPUs regardless of CLI input
    args.max_gpus = min(args.max_gpus, 2)

    # CUDA_VISIBLE_DEVICES MUST be set before any CUDA/torch initialisation.
    # If not already set (by the user or a previous exec), detect free GPUs via
    # nvidia-smi and re-exec this process with the env var baked in.  The
    # re-exec'd child will skip this branch (env var already set) and go
    # straight to train().
    if "CUDA_VISIBLE_DEVICES" not in os.environ:
        print("[GPU] Scanning available GPUs...")
        selected = select_free_gpus(max_gpus=args.max_gpus, min_free_gb=40.0)
        if not selected:
            print("[GPU] No GPU with >= 40 GB free. Aborting.")
            sys.exit(1)
        gpu_str = ",".join(str(g) for g in selected)
        print(f"[GPU] Re-launching with CUDA_VISIBLE_DEVICES={gpu_str}\n")
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = gpu_str
        os.execvpe(sys.executable, [sys.executable] + sys.argv, env)
        # execvpe replaces the current process — code below never runs

    print(f"[GPU] CUDA_VISIBLE_DEVICES={os.environ['CUDA_VISIBLE_DEVICES']}")
    train(args)
