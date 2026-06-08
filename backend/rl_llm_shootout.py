"""
Periodic LLM backend shootout — addresses the static `ollama_model` anti-pattern.

What this does:
  1. Pull rolling 7d holdout from rl_training_data.jsonl (records with realised
     reward_3d, action != HOLD, abs(reward) >= 0.5)
  2. Sample N records (default 60)
  3. For each candidate model: run the SAME deepseek_ai prompt path, score the
     prediction against actual reward_3d
  4. Compute directional_accuracy + mean_realised_reward
  5. Write report to rl_models/llm_shootout/YYYYMMDD_HHMMSS.json
  6. If winner != current `ollama_model` setting AND delta >= 5pp,
     update DB setting (auto-promotion)
  7. Email + WebSocket broadcast result (if wired by caller)

Candidates discovered from BOTH Ollama daemons:
  - 11434 (system) — deepseek-r1:32b, qwen2.5-coder:32b, llama3:8b, etc.
  - 11435 (user-space) — qwen3.5:35b
Default candidate list keeps only the reasoning-capable ones.
"""
from __future__ import annotations
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# Reasonable defaults: only models that have been shown viable on this task.
# Pulled from registry but trimmed to keep the shootout < 30 min.
DEFAULT_CANDIDATES = [
    ("qwen3.5:35b",    "http://localhost:11435"),
    ("deepseek-r1:32b","http://localhost:11434"),
    # Google Gemma 4 31B (= google/gemma-4-31B-it, Q4 quantized via Ollama).
    # Added 2026-05-26 per user — let the daily shootout decide if it beats
    # the incumbents on rolling-holdout directional accuracy.
    ("gemma4:31b",     "http://localhost:11435"),
]

PROMOTION_DELTA_PP = 5.0   # need ≥5pp dir_acc improvement to switch model


def _load_holdout(jsonl_path: str, days: int = 7, min_reward_abs: float = 0.5) -> list:
    cutoff = datetime.utcnow() - timedelta(days=days)
    out = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 1) Recent enough?
                try:
                    ts = datetime.fromisoformat(r.get("timestamp","").replace("Z",""))
                    if ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                # 2) Has usable reward?
                reward = r.get("reward_3d")
                if not isinstance(reward, (int,float)) or not math.isfinite(reward):
                    continue
                if abs(reward) < min_reward_abs:
                    continue
                # 3) Has non-HOLD action so reward signal is meaningful
                if r.get("action") == "HOLD":
                    continue
                # 4) Not a row contaminated by the broken-AI period
                rs = r.get("reasoning_summary") or ""
                if "Cannot connect" in rs or "分析出错" in rs:
                    continue
                # 5) Has state.price (needed to rebuild prompt)
                if not (r.get("state") or {}).get("price"):
                    continue
                out.append(r)
    except FileNotFoundError:
        logger.error(f"[Shootout] {jsonl_path} not found")
    return out


def _run_one_model(
    candidate_name: str,
    host: str,
    holdout: list,
    timeout: int = 90,
) -> dict:
    """Run one model on the holdout via the SAME production prompt path used by
    deepseek_ai.analyze_stock, so we measure production behavior not toy prompts."""
    import deepseek_ai as ai
    correct = 0
    invalid = 0
    realised_sum = 0.0
    acted = 0
    t_total = 0.0
    # Temporarily override model + host for each call
    orig_model_resolver = ai._get_model_name
    orig_host_resolver = ai._get_ollama_host
    ai._get_model_name = lambda provider: candidate_name
    ai._get_ollama_host = lambda: host
    try:
        for i, rec in enumerate(holdout):
            state = rec.get("state") or {}
            indicators = state.get("indicators") or {}
            # Rebuild minimal `quote` shape that analyze_stock expects.
            # We feed price + RSI + 52w + valuation_gap_pct etc. directly so
            # the prompt looks similar to production scans.
            quote = {
                "current": state.get("price"),
                "change": 0,
                "change_pct": state.get("change_pct", 0),
                "high": state.get("price"),
                "low": state.get("price"),
                "volume": state.get("volume", 0),
                "market_cap": state.get("market_cap", "N/A"),
                "pe_ratio": state.get("pe_ratio", "N/A"),
                "fifty_two_week_low": state.get("fifty_two_week_low", "N/A"),
                "fifty_two_week_high": state.get("fifty_two_week_high", "N/A"),
                # Intentionally OMIT dcf/ddm/intrinsic so the new sanity gate
                # hides them — match new production behavior.
                "valuation_gap_pct": state.get("valuation_gap_pct"),
                "vpa_signal": state.get("vpa_signal", "N/A"),
                "vpa_volume_ratio": state.get("vpa_volume_ratio", "N/A"),
                "liquidity": "N/A",
                "crowding": 0,
            }
            try:
                t0 = time.time()
                sig = ai.analyze_stock(
                    ai_provider="ollama",
                    api_key="",
                    symbol=rec.get("symbol","UNKNOWN"),
                    quote=quote,
                    indicators=indicators,
                    history=[],  # history not in holdout records
                    news=[],
                    portfolio_context="Backtest mode — no live portfolio.",
                    upcoming_events=rec.get("event_context_summary","")[:600],
                    rl_lessons="",
                    sector=rec.get("sector","Other"),
                    global_context=None,
                    catalysts=(rec.get("intelligence_metadata") or {}).get("catalysts", []),
                )
                t_total += (time.time() - t0)
                decision = (sig.get("signal") or "").upper()
            except Exception as e:
                invalid += 1
                logger.debug(f"[Shootout] {candidate_name} #{i}: {e}")
                continue
            if decision not in ("BUY","SELL","HOLD","COVER","SHORT"):
                invalid += 1
                continue
            # Normalize: treat COVER like BUY, SHORT like SELL
            if decision == "COVER": decision = "BUY"
            if decision == "SHORT": decision = "SELL"

            reward = rec["reward_3d"]
            if decision == "HOLD":
                if abs(reward) < 1.0:
                    correct += 1
                # HOLD didn't act
            elif decision == "BUY":
                acted += 1
                realised_sum += reward
                if reward > 0:
                    correct += 1
            elif decision == "SELL":
                acted += 1
                realised_sum += -reward
                if reward < 0:
                    correct += 1

            if (i+1) % 10 == 0:
                logger.info(f"[Shootout] {candidate_name} {i+1}/{len(holdout)} "
                            f"dir_acc={correct/max(1,(i+1)-invalid)*100:.1f}%")
    finally:
        # restore monkey-patched resolvers
        ai._get_model_name = orig_model_resolver
        ai._get_ollama_host = orig_host_resolver

    n_scored = len(holdout) - invalid
    return {
        "model": candidate_name,
        "host": host,
        "samples": len(holdout),
        "scored": n_scored,
        "invalid": invalid,
        "correct": correct,
        "acted": acted,
        "directional_accuracy": round(correct / max(1,n_scored) * 100, 2),
        "mean_realised_reward": round(realised_sum / max(1,acted), 4) if acted else 0.0,
        "mean_call_seconds": round(t_total / max(1,len(holdout)-invalid), 2),
    }


def run_shootout(
    rl_data_path: str | None = None,
    candidates: list | None = None,
    sample_n: int = 80,
    days: int = 30,           # was 7; widened because 5/15→5/20 broken-AI
                              # period contaminated last week's data
    auto_promote: bool = True,
    db_session=None,
) -> dict:
    """Main entry. Returns shootout report dict."""
    if rl_data_path is None:
        rl_data_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rl_training_data.jsonl"
        )
    if candidates is None:
        candidates = DEFAULT_CANDIDATES

    holdout = _load_holdout(rl_data_path, days=days)
    if len(holdout) < 20:
        logger.warning(f"[Shootout] only {len(holdout)} usable holdout records — skipping")
        return {"error": f"only {len(holdout)} usable records"}

    random.seed(42)
    random.shuffle(holdout)
    holdout = holdout[:sample_n]
    logger.info(f"[Shootout] running on {len(holdout)} records, {len(candidates)} candidates")

    results = []
    for name, host in candidates:
        # Quick liveness check
        try:
            import requests
            r = requests.get(f"{host}/api/tags", timeout=5)
            if r.status_code != 200:
                logger.warning(f"[Shootout] {name}@{host}: not reachable (HTTP {r.status_code})")
                results.append({"model": name, "host": host, "error": "host unreachable"})
                continue
        except Exception as e:
            logger.warning(f"[Shootout] {name}@{host}: ping failed — {e}")
            results.append({"model": name, "host": host, "error": f"ping failed: {e}"})
            continue
        logger.info(f"[Shootout] testing {name}@{host}...")
        r = _run_one_model(name, host, holdout)
        results.append(r)
        logger.info(f"[Shootout] {name} → dir_acc={r.get('directional_accuracy')}%, "
                    f"reward={r.get('mean_realised_reward')}, "
                    f"latency={r.get('mean_call_seconds')}s")

    # Pick winner
    scored = [r for r in results if "directional_accuracy" in r and r["scored"] >= 10]
    winner = None
    if scored:
        winner = max(scored, key=lambda r: (r["directional_accuracy"], r["mean_realised_reward"]))

    report = {
        "ran_at": datetime.utcnow().isoformat(),
        "sample_size": len(holdout),
        "holdout_days": days,
        "results": results,
        "winner": winner["model"] if winner else None,
        "winner_metrics": winner,
    }

    # Auto-promote (decide first, then persist so report contains promotion field)
    if auto_promote and winner and db_session is not None:
        from database import Settings
        cur = db_session.query(Settings).filter(Settings.key=="ollama_model").first()
        if cur and cur.value != winner["model"]:
            # Compute delta vs current
            cur_metrics = next((r for r in scored if r["model"]==cur.value), None)
            cur_acc = cur_metrics["directional_accuracy"] if cur_metrics else 0
            delta_pp = winner["directional_accuracy"] - cur_acc
            if delta_pp >= PROMOTION_DELTA_PP:
                logger.info(f"[Shootout] PROMOTING {cur.value} → {winner['model']} "
                            f"({delta_pp:+.1f}pp dir_acc)")
                cur.value = winner["model"]
                # Also update ollama_host to wherever winner lives
                host_row = db_session.query(Settings).filter(Settings.key=="ollama_host").first()
                if host_row:
                    host_row.value = winner["host"]
                db_session.commit()
                report["promoted"] = True
                report["promoted_from"] = cur.value
                report["promoted_to"] = winner["model"]
                report["delta_pp"] = round(delta_pp, 2)
            else:
                logger.info(f"[Shootout] winner is {winner['model']} but only "
                            f"+{delta_pp:.1f}pp over current ({cur.value}) — "
                            f"below {PROMOTION_DELTA_PP}pp threshold, no promotion")
                report["promoted"] = False
                report["delta_pp"] = round(delta_pp, 2)
        else:
            report["promoted"] = False
            report["already_winner"] = True

    # Persist (after promotion decision so JSON reflects final state)
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rl_models", "llm_shootout"
    )
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info(f"[Shootout] report saved → {out_path}")
    with open(os.path.join(out_dir, "latest.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from database import SessionLocal
    db = SessionLocal()
    rpt = run_shootout(db_session=db)
    print("\n=== SHOOTOUT RESULT ===")
    print(json.dumps(rpt, indent=2))
