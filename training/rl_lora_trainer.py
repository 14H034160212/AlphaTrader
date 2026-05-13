"""
AlphaTrader RL LoRA Trainer (Path 2)
=====================================
Fine-tunes Qwen2.5-32B (or any compatible Qwen model) with LoRA adapters
using reward-weighted SFT on AlphaTrader's real trading P&L data.

Strategy: Reward-weighted SFT (offline RL)
  - Records where reward_3d > 0 (correct signals) → high loss weight
  - Records where reward_3d < 0 (wrong signals)   → near-zero loss weight
  - Result: model learns to produce signals that actually made money

Why reward-weighted SFT instead of GRPO/PPO first:
  - 39k labeled records already exist — no need for online rollouts
  - Stable training, no reward hacking risk
  - Can upgrade to GRPO after validating the adapter

Hardware: 8x A100 80GB — use FSDP + gradient checkpointing for 32B model
LoRA config: r=16, alpha=32, ~1% of parameters — fits in 2x A100 per replica

Usage:
  # Step 1: prepare dataset (run once)
  python prepare_rl_dataset.py --rl_data ../rl_training_data.jsonl --output ./rl_sft_dataset

  # Step 2: train (multi-GPU)
  torchrun --nproc_per_node=8 rl_lora_trainer.py \
      --model_name_or_path Qwen/Qwen2.5-32B-Instruct \
      --dataset_dir ./rl_sft_dataset \
      --output_dir ./lora_checkpoints \
      --num_epochs 3 \
      --per_device_batch_size 2 \
      --gradient_accumulation_steps 4

  # Step 3: deploy adapter
  # Copy lora_checkpoints/final/ to ollama model path or load via vLLM
"""

import argparse
import json
import math
import os
import sys

import torch
from torch.utils.data import Dataset, DataLoader


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

        # Format as chat completion
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

        # Labels: only compute loss on the response tokens (mask the prompt)
        prompt_enc = self.tokenizer(
            f"{prompt}\n<|assistant|>\n",
            max_length=self.max_length,
            truncation=True,
        )
        prompt_len = len(prompt_enc["input_ids"])

        labels = input_ids.clone()
        labels[:prompt_len] = -100   # ignore loss on prompt tokens
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
    """
    Per-sample reward-weighted cross-entropy.
    weights: (batch,) tensor, one weight per sample
    """
    loss_fn = torch.nn.CrossEntropyLoss(reduction="none", ignore_index=-100)
    B, T, V = logits.shape
    # shift for next-token prediction
    shift_logits = logits[..., :-1, :].contiguous().view(-1, V)
    shift_labels = labels[..., 1:].contiguous().view(-1)

    token_loss = loss_fn(shift_logits, shift_labels)         # (B*T,)
    token_loss = token_loss.view(B, T - 1)                   # (B, T-1)

    # Mean over non-ignored tokens per sample
    valid_mask  = (labels[..., 1:] != -100).float()          # (B, T-1)
    sample_loss = (token_loss * valid_mask).sum(dim=1) / valid_mask.sum(dim=1).clamp(min=1)

    # Weight by RL reward signal
    w = weights.to(sample_loss.device)
    return (sample_loss * w).mean()


# ──────────────────────────────────────────────
# Main training loop
# ──────────────────────────────────────────────

def train(args):
    from transformers import AutoTokenizer, AutoModelForCausalLM, get_linear_schedule_with_warmup
    from peft import LoraConfig, get_peft_model, TaskType

    local_rank  = int(os.environ.get("LOCAL_RANK", 0))
    world_size  = int(os.environ.get("WORLD_SIZE", 1))
    is_main     = (local_rank == 0)

    if world_size > 1:
        torch.distributed.init_process_group("nccl")
    torch.cuda.set_device(local_rank)
    device = torch.device(f"cuda:{local_rank}")

    if is_main:
        print(f"[Trainer] Loading tokenizer from {args.model_name_or_path}")

    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        trust_remote_code=True,
        padding_side="right",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if is_main:
        print(f"[Trainer] Loading model (this takes a few minutes for 32B)...")

    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        device_map={"": device},
    )
    model.gradient_checkpointing_enable()

    # LoRA configuration
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )
    model = get_peft_model(model, lora_config)
    if is_main:
        model.print_trainable_parameters()

    # Wrap in DDP if multi-GPU
    if world_size > 1:
        from torch.nn.parallel import DistributedDataParallel as DDP
        model = DDP(model, device_ids=[local_rank], output_device=local_rank)

    # Dataset
    train_ds = RLSFTDataset(
        os.path.join(args.dataset_dir, "train.jsonl"),
        tokenizer,
        max_length=args.max_length,
    )
    val_ds = RLSFTDataset(
        os.path.join(args.dataset_dir, "val.jsonl"),
        tokenizer,
        max_length=args.max_length,
    )

    train_sampler = (
        torch.utils.data.distributed.DistributedSampler(train_ds) if world_size > 1 else None
    )
    train_loader = DataLoader(
        train_ds,
        batch_size=args.per_device_batch_size,
        sampler=train_sampler,
        shuffle=(train_sampler is None),
        num_workers=4,
        pin_memory=True,
    )
    val_loader = DataLoader(val_ds, batch_size=args.per_device_batch_size, num_workers=2)

    optimizer = torch.optim.AdamW(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.learning_rate,
        weight_decay=0.01,
    )

    total_steps   = len(train_loader) * args.num_epochs // args.gradient_accumulation_steps
    warmup_steps  = max(1, total_steps // 10)
    scheduler     = get_linear_schedule_with_warmup(optimizer, warmup_steps, total_steps)

    os.makedirs(args.output_dir, exist_ok=True)
    best_val_loss = float("inf")

    for epoch in range(args.num_epochs):
        model.train()
        if train_sampler:
            train_sampler.set_epoch(epoch)

        total_loss = 0.0
        optimizer.zero_grad()

        for step, batch in enumerate(train_loader):
            input_ids      = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels         = batch["labels"].to(device)
            weights        = batch["weight"].to(device)

            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            loss    = weighted_cross_entropy(outputs.logits, labels, weights)
            loss    = loss / args.gradient_accumulation_steps
            loss.backward()
            total_loss += loss.item() * args.gradient_accumulation_steps

            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

                if is_main and (step + 1) % (args.gradient_accumulation_steps * 10) == 0:
                    avg = total_loss / (step + 1)
                    print(f"  Epoch {epoch+1} step {step+1}/{len(train_loader)}  loss={avg:.4f}")

        # Validation
        if is_main:
            model.eval()
            val_loss = 0.0
            with torch.no_grad():
                for batch in val_loader:
                    input_ids      = batch["input_ids"].to(device)
                    attention_mask = batch["attention_mask"].to(device)
                    labels         = batch["labels"].to(device)
                    weights        = batch["weight"].to(device)
                    outputs        = model(input_ids=input_ids, attention_mask=attention_mask)
                    val_loss      += weighted_cross_entropy(outputs.logits, labels, weights).item()
            val_loss /= len(val_loader)
            print(f"Epoch {epoch+1} complete — train_loss={total_loss/len(train_loader):.4f}  val_loss={val_loss:.4f}")

            ckpt_dir = os.path.join(args.output_dir, f"epoch_{epoch+1}")
            raw_model = model.module if hasattr(model, "module") else model
            raw_model.save_pretrained(ckpt_dir)
            tokenizer.save_pretrained(ckpt_dir)
            print(f"  Checkpoint saved → {ckpt_dir}")

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                final_dir = os.path.join(args.output_dir, "best")
                raw_model.save_pretrained(final_dir)
                tokenizer.save_pretrained(final_dir)
                print(f"  New best val_loss={val_loss:.4f} → {final_dir}")

    if is_main:
        print(f"\n[Trainer] Done. Best val_loss={best_val_loss:.4f}")
        print(f"  Best adapter: {os.path.join(args.output_dir, 'best')}")
        print("\nNext steps:")
        print("  1. Merge adapter: python merge_lora.py --base <model> --adapter ./lora_checkpoints/best")
        print("  2. Or load directly: peft.PeftModel.from_pretrained(base_model, './lora_checkpoints/best')")

    if world_size > 1:
        torch.distributed.destroy_process_group()


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlphaTrader RL LoRA Trainer")
    parser.add_argument("--model_name_or_path", default="Qwen/Qwen2.5-32B-Instruct",
                        help="HuggingFace model ID or local path")
    parser.add_argument("--dataset_dir",        default="./rl_sft_dataset",
                        help="Directory with train.jsonl and val.jsonl")
    parser.add_argument("--output_dir",         default="./lora_checkpoints")
    parser.add_argument("--num_epochs",         type=int,   default=3)
    parser.add_argument("--per_device_batch_size", type=int, default=2)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=4)
    parser.add_argument("--learning_rate",      type=float, default=2e-4)
    parser.add_argument("--max_length",         type=int,   default=2048)
    args = parser.parse_args()

    train(args)
