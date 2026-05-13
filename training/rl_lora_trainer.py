"""
AlphaTrader RL LoRA Trainer (Path 2)
=====================================
Fine-tunes Qwen3.5-35B-A3B (MoE) with LoRA adapters using reward-weighted SFT
on AlphaTrader's real trading P&L data.

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

        full_text = f"{prompt}\n<|assistant|>\n{response}"

        enc = self.tokenizer(
            full_text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        input_ids      = enc["input_ids"].squeeze(0)
        attention_mask = enc["attention_mask"].squeeze(0)

        # Only compute loss on response tokens
        prompt_enc = self.tokenizer(
            f"{prompt}\n<|assistant|>\n",
            max_length=self.max_length,
            truncation=True,
        )
        prompt_len = len(prompt_enc["input_ids"])

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

    # ── GPU selection ──────────────────────────────────────
    if "CUDA_VISIBLE_DEVICES" in os.environ:
        # User overrode manually
        visible = [int(x) for x in os.environ["CUDA_VISIBLE_DEVICES"].split(",") if x.strip()]
        print(f"[GPU] CUDA_VISIBLE_DEVICES override: {visible}")
        selected_gpus = visible[:args.max_gpus]
    else:
        print("[GPU] Scanning available GPUs...")
        selected_gpus = select_free_gpus(max_gpus=args.max_gpus, min_free_gb=40.0)
        if not selected_gpus:
            print("[GPU] No GPU with >= 40GB free found. Aborting.")
            sys.exit(1)
        # Set env var so PyTorch only sees these GPUs
        os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in selected_gpus)
        print(f"[GPU] Set CUDA_VISIBLE_DEVICES={os.environ['CUDA_VISIBLE_DEVICES']}")

    num_gpus = torch.cuda.device_count()  # after CUDA_VISIBLE_DEVICES is set
    print(f"[GPU] Training on {num_gpus} GPU(s): {selected_gpus[:num_gpus]}")

    # With 2 GPUs and a 32B model: device_map="auto" splits the model across
    # both GPUs (pipeline parallelism). Single process — no torchrun needed.
    device_map = "auto" if num_gpus > 1 else "cuda:0"

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
        torch_dtype=torch.bfloat16,
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
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    # Print per-GPU memory after loading
    for i in range(num_gpus):
        alloc = torch.cuda.memory_allocated(i) / 1e9
        total = torch.cuda.get_device_properties(i).total_memory / 1e9
        print(f"  GPU {selected_gpus[i]}: {alloc:.1f} / {total:.1f} GB used after model load")

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

    for epoch in range(args.num_epochs):
        model.train()
        total_loss = 0.0
        optimizer.zero_grad()

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

            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(
                    filter(lambda p: p.requires_grad, model.parameters()), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

                if (step + 1) % (args.gradient_accumulation_steps * 10) == 0:
                    avg = total_loss / (step + 1)
                    lr  = scheduler.get_last_lr()[0]
                    print(f"  Epoch {epoch+1} step {step+1}/{len(train_loader)}  "
                          f"loss={avg:.4f}  lr={lr:.2e}")

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
    parser = argparse.ArgumentParser(description="AlphaTrader RL LoRA Trainer")
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
    args = parser.parse_args()

    # Safety cap — never exceed 2 GPUs regardless of CLI input
    args.max_gpus = min(args.max_gpus, 2)

    train(args)
