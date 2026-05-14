# AlphaTrader RL Pipeline — Operations & Methodology

End-to-end MLOps for the trading signal generator. Read this before
changing models, thresholds, or training schedules.

## Architecture

```
   Live trade flow                       Continuous learning
   ─────────────────                     ──────────────────────
   Ollama (Qwen3.5)        →             rl_data_collector.py
        ↓                                JSONL: signal + reward_3d
   deepseek_ai.analyze_stock                  ↓
        ↓                                rl_pipeline.run_cycle()  (every 6h)
   XGBoost veto filter                        ↓
        ↓                                ├─ train candidate XGBoost
   Kelly position sizing                ├─ score vs production on holdout
        ↓                                ├─ promote / shadow / reject
   Alpaca / Futu                            ├─ mine hard examples
                                            └─ trigger LoRA if +2000 new records
                                                  ↓
                                            rl_lora_trainer.py  (~3 days)
                                                  ↓
                                            rl_lora_validator.py
                                                  ↓
                                            rl_lora_deploy.py → vLLM:11436
                                                  ↓
                                            deepseek_ai.py auto-routes
```

## Test Sets (never put these in training)

| Set | Purpose | Source | Refresh |
|-----|---------|--------|---------|
| **Rolling holdout** | Apples-to-apples comparison | Last 7 days of JSONL | Continuously |
| **Challenge set** | Permanent hard examples | Production failures, `|reward|>=2%` | Append-only, max 1000 |

Both are excluded from training via `prepare_rl_dataset.py --exclude_last_days 7 --exclude_challenge_set`.

## Decision Rules

### XGBoost candidate vs production
- `dir_acc_pp >= +2.0` AND `rmse <= 0`  → **promote** to production
- `dir_acc_pp > 0` but < +2.0           → **shadow** (parallel logging 7 days)
- otherwise                              → **reject**

### LoRA candidate vs production live
- `dir_acc_pp >= +5.0` AND realised reward > 0 → **deploy** (only if `lora_auto_deploy_enabled=true`)
- `dir_acc_pp > 0` but < +5.0                 → **shadow** (report only)
- otherwise                                    → **reject**

## Lessons Learned (the hard way)

### 1. Sample size matters
- 30-sample comparison said Qwen3.5 beat DeepSeek-R1 63% vs 61%
- 200-sample on same challenge set: both 39.5% (noise)
- 300-sample fair holdout: 65% vs 50% (real signal)
- **Minimum 200 samples** before believing any model A vs B verdict

### 2. Challenge set is biased BY DESIGN
- It's selected to be examples where production failed
- "Always BUY" on this set hits 39.5% (= % of records where production said SELL but it went up)
- Use challenge set as a **stress test** for hard cases, not as the only benchmark
- Always pair with rolling holdout for full picture

### 3. Raw LLM doesn't generate alpha
- Qwen3.5 alone: 100% BUY across 300 samples → behaves like Always-BUY
- Qwen2.5 alone: 93% HOLD across 300 samples → behaves like Always-HOLD
- Alpha comes from: **prompt engineering in `deepseek_ai.py`** + **XGBoost veto filter**
- A different base LLM mostly affects (a) speed and (b) downstream LoRA potential

### 4. Reasoning models need 4096+ tokens
- Qwen3.5 / DeepSeek-R1 / *-r1 / *-thinking models emit large thinking traces
- `_call_ollama()` auto-detects via name pattern and bumps `num_predict` to 4096
- Fallback: if `content` is empty, use the `thinking` field

### 5. CUDA_VISIBLE_DEVICES must be set before torch import
- `os.environ["CUDA_VISIBLE_DEVICES"]` in mid-script does NOT take effect
- `rl_lora_trainer.py` re-execs itself with the env var pre-set
- This bit us once and cost a wasted training run

## Operating the Pipeline

### Daily monitoring
```bash
# Pipeline state
curl http://localhost:8888/api/rl/pipeline

# Model registry (every candidate ever trained)
curl http://localhost:8888/api/rl/models

# Apples-to-apples comparison
curl http://localhost:8888/api/rl/compare

# Production weak spots
curl http://localhost:8888/api/rl/errors

# Challenge set growth
curl http://localhost:8888/api/rl/challenge

# LoRA service
curl http://localhost:8888/api/rl/lora/status
```

### Manual controls
```bash
# Force a pipeline cycle now
curl -X POST http://localhost:8888/api/rl/pipeline/run

# Trigger a model shootout (~30 min)
curl -X POST 'http://localhost:8888/api/rl/shootout?test_set=challenge'

# Enable LoRA auto-deployment
curl -X POST http://localhost:8888/api/rl/lora/auto-deploy/true

# Manually deploy current adapter
curl -X POST http://localhost:8888/api/rl/lora/deploy

# Roll back LoRA (revert to Ollama)
curl -X POST http://localhost:8888/api/rl/lora/rollback

# Promote shadow XGBoost to production
curl -X POST http://localhost:8888/api/rl/promote-shadow
```

### Swapping the base LLM
```python
# In backend/ with the alphatrader env active:
python -c "
from database import SessionLocal, set_setting
db = SessionLocal()
set_setting(db, 'ollama_host',  'http://localhost:11435', 1)  # 11434 = system, 11435 = user
set_setting(db, 'ollama_model', 'qwen3.5:35b',            1)
db.close()
"
# Then restart backend (kills python, start.sh while-loop respawns):
pkill -9 -f "uvicorn.run.*8888"
```

## Files

```
backend/
  rl_data_collector.py    JSONL writer + reward backfill
  rl_policy_model.py      XGBoost + versioning + production/shadow split
  rl_validation.py        holdout split, metrics, promote/shadow/reject decision
  rl_pipeline.py          orchestrator — runs every 6h
  rl_compare.py           apples-to-apples scoring across XGBoost/LoRA/baselines
  rl_challenge_set.py     hard-example mining + error analysis
  rl_lora_validator.py    score base+adapter on holdout
  rl_lora_deploy.py       merge adapter + start vLLM:11436
  rl_raw_model_validator.py  call Ollama/vLLM/HF for any model, score uniformly

training/
  prepare_rl_dataset.py   JSONL → SFT pairs, excludes holdout + challenge set
  rl_lora_trainer.py      Qwen3.5-35B-A3B LoRA + reward-weighted SFT

rl_models/                (gitignored)
  registry.json           every candidate's metrics + decision
  xgb_v*.pkl              versioned XGBoost models
  challenge_test_set.jsonl permanent hard examples
  raw_model_reports/      shootout JSON dumps
  error_analysis.json     sector/action accuracy breakdown
```

## Current Production Setup

- **LLM**: Qwen3.5:35b via user-Ollama on port 11435 (`ollama_host`)
- **Reasoning fallback**: empty `content` → uses `thinking` trace
- **XGBoost filter**: vetoes BUY if `predicted_3d_reward < -1%` AND `confidence < 0.85`
- **LoRA**: training in progress (PID 2481338); will auto-deploy if `lora_auto_deploy_enabled=true` AND validation passes
- **Pipeline cadence**: 6 hours per cycle
- **LoRA trigger**: +2000 new labeled records since last training
