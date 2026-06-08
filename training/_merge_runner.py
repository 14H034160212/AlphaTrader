"""LoRA merge runner (spawned as a subprocess from rl_lora_deploy.py)."""
import argparse, os, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

parser = argparse.ArgumentParser()
parser.add_argument("--base", required=True)
parser.add_argument("--adapter", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

print(f"Loading base: {args.base}", flush=True)
base = AutoModelForCausalLM.from_pretrained(
    args.base, dtype=torch.bfloat16, trust_remote_code=True,
    device_map="auto",
)
tok = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)

print(f"Loading adapter: {args.adapter}", flush=True)
model = PeftModel.from_pretrained(base, args.adapter)

print("Merging...", flush=True)
merged = model.merge_and_unload()

print(f"Saving to: {args.output}", flush=True)
merged.save_pretrained(args.output, safe_serialization=True)
tok.save_pretrained(args.output)
print("DONE", flush=True)
