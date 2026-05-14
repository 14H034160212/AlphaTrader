"""
LoRA Adapter Deployer
======================
Once validate_lora() approves a new adapter, this module merges it into the
base model and serves it via a vLLM server on port 11500.  Updates the DB
setting `lora_inference_url` so deepseek_ai.py routes new signals through
the fine-tuned model.

Pipeline:
  1. peft merge_and_unload  → save merged model to rl_models/lora_merged_{ver}/
  2. Stop any running vLLM service on the LoRA port (graceful)
  3. Spawn vLLM with the merged model as a background subprocess
  4. Poll healthcheck until /v1/models returns OK (or 5 min timeout)
  5. Update DB settings:
        lora_inference_url=http://localhost:11500/v1
        lora_model_version={version}
  6. deepseek_ai.py reads these on the next analysis call

vLLM is the same one already running for VLLM::Worker_TP on GPU 3+4 — we
launch ours on a separate port (11500) and a separate GPU pair (1+2 once
training finishes) so it doesn't compete.
"""
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

logger = logging.getLogger(__name__)

LORA_VLLM_PORT       = 11500     # NOTE: 11436 was taken by another Ollama (deepseek-r1:70b)
LORA_VLLM_GPUS       = "1,2"     # use the same GPUs that did training
LORA_VLLM_LOG        = "/tmp/lora_vllm.log"
LORA_VLLM_PID_FILE   = "/tmp/lora_vllm.pid"


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _read_pid() -> int | None:
    if not os.path.exists(LORA_VLLM_PID_FILE):
        return None
    try:
        with open(LORA_VLLM_PID_FILE) as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def stop_lora_vllm() -> bool:
    """Stop any running LoRA vLLM service.  Returns True if something was killed."""
    pid = _read_pid()
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            logger.info(f"[LoRA Deploy] Sent SIGTERM to vLLM group {pid}")
            # Wait up to 30s for graceful shutdown
            for _ in range(30):
                if not _is_port_in_use(LORA_VLLM_PORT):
                    break
                time.sleep(1)
            if _is_port_in_use(LORA_VLLM_PORT):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                logger.warning(f"[LoRA Deploy] Force-killed vLLM {pid}")
            os.remove(LORA_VLLM_PID_FILE)
            return True
        except ProcessLookupError:
            os.remove(LORA_VLLM_PID_FILE)
        except Exception as e:
            logger.error(f"[LoRA Deploy] Stop failed: {e}")
    return False


def merge_adapter(
    base_model: str,
    adapter_path: str,
    output_dir: str,
    cuda_visible_devices: str = LORA_VLLM_GPUS,
) -> str | None:
    """
    Merge LoRA adapter into base weights, save the standalone merged model.
    Returns the output path, or None on failure.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Run merge in a subprocess so we can release GPU memory after it finishes
    # (peft + transformers don't always clean up cleanly otherwise).
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    merge_script = os.path.join(repo_root, "training", "_merge_runner.py")
    if not os.path.exists(merge_script):
        # Create the merge runner on demand
        _create_merge_runner(merge_script)

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    logger.info(f"[LoRA Deploy] Merging adapter → {output_dir} ...")
    try:
        result = subprocess.run(
            [sys.executable, merge_script,
             "--base", base_model,
             "--adapter", adapter_path,
             "--output", output_dir],
            env=env,
            check=True,
            timeout=3600,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info(f"[LoRA Deploy] Merge done: {result.stdout[-500:]}")
        return output_dir
    except subprocess.CalledProcessError as e:
        logger.error(f"[LoRA Deploy] Merge failed: {e.stdout[-2000:]}")
        return None


def _create_merge_runner(path: str):
    """Drop a small standalone script that does the merge."""
    code = '''"""LoRA merge runner (spawned as a subprocess from rl_lora_deploy.py)."""
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
'''
    with open(path, "w") as f:
        f.write(code)


def start_lora_vllm(merged_model_path: str) -> bool:
    """
    Spawn a vLLM server on LORA_VLLM_PORT with the merged model.
    Returns True once the server is healthy, False on timeout.
    """
    # vLLM Python module must be importable
    try:
        import vllm    # noqa: F401
    except ImportError:
        logger.error("[LoRA Deploy] vllm not installed — run `pip install vllm`")
        return False

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = LORA_VLLM_GPUS
    env["PYTHONUNBUFFERED"]     = "1"

    # Launch vLLM OpenAI-compatible server in its own process group
    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", merged_model_path,
        "--port", str(LORA_VLLM_PORT),
        "--host", "127.0.0.1",
        "--gpu-memory-utilization", "0.85",
        "--tensor-parallel-size", "2",
        "--max-model-len", "4096",
        "--served-model-name", "alphatrader-lora",
        "--trust-remote-code",
    ]
    logger.info(f"[LoRA Deploy] Launching vLLM: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=open(LORA_VLLM_LOG, "a"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    with open(LORA_VLLM_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    logger.info(f"[LoRA Deploy] vLLM PID {proc.pid}, waiting for healthcheck...")

    # Poll healthcheck (vLLM is slow to start with 35B model — 3-5 min)
    import requests
    deadline = time.time() + 600    # 10 min
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.error(f"[LoRA Deploy] vLLM process died early (rc={proc.returncode})")
            return False
        try:
            r = requests.get(f"http://127.0.0.1:{LORA_VLLM_PORT}/v1/models", timeout=2)
            if r.status_code == 200:
                logger.info("[LoRA Deploy] vLLM healthy ✓")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(5)
    logger.error("[LoRA Deploy] vLLM healthcheck timed out (10 min)")
    return False


def deploy_adapter(
    adapter_path: str,
    version: str,
    base_model: str = "Qwen/Qwen3.5-35B-A3B",
) -> dict:
    """
    Full deployment: merge + start vLLM + update DB.
    Returns {"status": "ok" | "failed", "url", "version", "error" (opt)}.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    merged_dir = os.path.join(repo_root, "rl_models", f"lora_merged_{version}")

    # Stop any existing LoRA vLLM
    stop_lora_vllm()

    # Merge
    merged = merge_adapter(base_model, adapter_path, merged_dir)
    if merged is None:
        return {"status": "failed", "error": "merge failed"}

    # Launch
    if not start_lora_vllm(merged):
        return {"status": "failed", "error": "vLLM healthcheck failed"}

    # Update DB
    try:
        from database import get_db, set_setting
        db = next(get_db())
        try:
            url = f"http://127.0.0.1:{LORA_VLLM_PORT}/v1"
            set_setting(db, "lora_inference_url",   url,     1)
            set_setting(db, "lora_model_version",   version, 1)
            set_setting(db, "lora_deployed_at",     datetime.utcnow().isoformat(), 1)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[LoRA Deploy] DB update failed: {e}")

    return {
        "status":  "ok",
        "url":     f"http://127.0.0.1:{LORA_VLLM_PORT}/v1",
        "version": version,
        "merged_path": merged,
    }


def rollback_lora() -> dict:
    """Disable LoRA routing and stop the vLLM service."""
    stop_lora_vllm()
    try:
        from database import get_db, set_setting
        db = next(get_db())
        try:
            set_setting(db, "lora_inference_url", "", 1)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[LoRA Deploy] DB clear failed: {e}")
    return {"status": "rolled_back"}
