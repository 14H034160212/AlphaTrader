"""
Intelligence Feedback Loop
Analyzes collected RL data to attribute P&L to specific intelligence sources.
Helps identify which catalysts or macros are most predictive of price movements.
"""
import json
import os
import logging
from datetime import datetime

# Path to the RL data file (same as in rl_data_collector.py)
RL_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "rl_training_data.jsonl")
REPORT_FILE = os.path.join(os.path.dirname(__file__), "..", "intelligence_attribution_report.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_attribution_analysis():
    """
    Scans RL_DATA_FILE for records with outcomes and calculates attribution scores.
    """
    if not os.path.exists(RL_DATA_FILE):
        logger.warning(f"No RL data file found at {RL_DATA_FILE}")
        return

    records = []
    with open(RL_DATA_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue

    # Filter to records that have outcomes
    processed_records = [r for r in records if r.get("outcome_filled")]
    if not processed_records:
        logger.info("No records with outcomes yet. Run rl_data_collector.update_trade_outcomes() first.")
        return

    stats = {
        "total_analyzed": len(processed_records),
        "catalyst_performance": {}, # keyword -> {sum_reward, count, avg}
        "macro_performance": {},    # scenario_id -> {sum_reward, count, avg}
        "accuracy_by_confidence": {
            "high": {"correct": 0, "total": 0},
            "medium": {"correct": 0, "total": 0},
            "low": {"correct": 0, "total": 0}
        }
    }

    for rec in processed_records:
        reward = rec.get("reward_1d")
        if reward is None:
            continue
        
        confidence = rec.get("confidence", 0)
        conf_bracket = "high" if confidence >= 0.8 else "medium" if confidence >= 0.5 else "low"
        stats["accuracy_by_confidence"][conf_bracket]["total"] += 1
        if reward > 0:
            stats["accuracy_by_confidence"][conf_bracket]["correct"] += 1

        intel = rec.get("intelligence_metadata", {})
        
        # Attribute to catalysts
        for cat in intel.get("catalysts", []):
            for kw in cat.get("matched_keywords", []):
                if kw not in stats["catalyst_performance"]:
                    stats["catalyst_performance"][kw] = {"sum_reward": 0.0, "count": 0}
                stats["catalyst_performance"][kw]["sum_reward"] += reward
                stats["catalyst_performance"][kw]["count"] += 1
        
        # Attribute to macros
        for macro in intel.get("macros", []):
            m_id = macro.get("scenario_id", "unknown")
            if m_id not in stats["macro_performance"]:
                stats["macro_performance"][m_id] = {"sum_reward": 0.0, "count": 0}
            stats["macro_performance"][m_id]["sum_reward"] += reward
            stats["macro_performance"][m_id]["count"] += 1

    # Finalize averages
    for kw, val in stats["catalyst_performance"].items():
        val["avg_reward"] = round(val["sum_reward"] / val["count"], 4)
    for m_id, val in stats["macro_performance"].items():
        val["avg_reward"] = round(val["sum_reward"] / val["count"], 4)

    # Save report
    with open(REPORT_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    
    logger.info(f"Attribution report saved to {REPORT_FILE}")
    return stats

if __name__ == "__main__":
    result = run_attribution_analysis()
    if result:
        print("\n=== Intelligence Attribution Summary ===")
        print(f"Total Records Analyzed: {result['total_analyzed']}")
        
        print("\n--- Top Performing Catalyst Keywords (Avg 1d Reward) ---")
        sorted_cats = sorted(result["catalyst_performance"].items(), key=lambda x: x[1]["avg_reward"], reverse=True)
        for kw, data in sorted_cats[:10]:
            print(f"  {kw}: {data['avg_reward']}% (based on {data['count']} signals)")
            
        print("\n--- Macro Scenario Impact ---")
        for m_id, data in result["macro_performance"].items():
            print(f"  {m_id}: {data['avg_reward']}% (based on {data['count']} signals)")
