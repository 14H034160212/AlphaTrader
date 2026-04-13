"""
Scenario Lifecycle Manager — 场景生命周期管理器

Replaces static hardcoded MACRO_SCENARIOS with a dynamic, DB-backed
lifecycle system that can:
  1. Detect scenario resolution via deterministic keywords (Layer 1)
  2. Periodically review scenarios via AI (Layer 2)
  3. Store lifecycle state in DB (Layer 3)
  4. Auto-generate new scenarios from news (Layer 4)

Lifecycle states: ACTIVE → DECLINING → RESOLVED / EXPIRED
Severity levels:  CRITICAL > HIGH > MEDIUM > LOW > BULLISH
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Severity ordering for escalation / de-escalation
SEVERITY_ORDER = ["BULLISH", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

# ── Cooldowns & Thresholds ──────────────────────────────────────────────────
RESOLUTION_DECLINING_THRESHOLD = 2   # resolution keyword hits before DECLINING
RESOLUTION_RESOLVED_THRESHOLD = 4    # resolution keyword hits before RESOLVED
DECAY_SEVERITY_DROP_MISSES = 18      # 18 × 10 min = 3 hours with no evidence
DECAY_DECLINING_MISSES = 36          # 6 hours
DECAY_EXPIRED_MISSES = 72            # 12 hours
REACTIVATION_COOLDOWN_SECONDS = 1200  # 20 min cooldown before DECLINING→ACTIVE


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: DB Seed & Read
# ═══════════════════════════════════════════════════════════════════════════

def seed_scenarios_from_hardcoded(db: Session):
    """
    Populate scenario_states table from MACRO_SCENARIOS on first boot.
    Only inserts scenarios that don't already exist (preserves DB modifications).
    """
    from database import ScenarioState
    from news_intelligence import MACRO_SCENARIOS

    existing_ids = {
        row.scenario_id
        for row in db.query(ScenarioState.scenario_id).all()
    }

    now = datetime.utcnow()
    added = 0
    for scenario_id, data in MACRO_SCENARIOS.items():
        if scenario_id in existing_ids:
            continue
        row = ScenarioState(
            scenario_id=scenario_id,
            name=data.get("name", scenario_id),
            description=data.get("description", ""),
            severity=data.get("severity", "MEDIUM"),
            lifecycle_state="ACTIVE",
            origin="seed",
            trigger_keywords_json=json.dumps(data.get("trigger_keywords", [])),
            resolution_keywords_json=json.dumps(data.get("resolution_keywords", [])),
            stocks_to_avoid_json=json.dumps(data.get("stocks_to_avoid", [])),
            potential_beneficiaries_json=json.dumps(data.get("potential_beneficiaries", [])),
            sectors_at_risk_json=json.dumps(data.get("sectors_at_risk", [])),
            first_detected_at=now,
            evidence_count=0,
            resolution_evidence_count=0,
            consecutive_misses=0,
        )
        db.add(row)
        added += 1

    if added:
        db.commit()
        logger.info(f"[ScenarioLifecycle] Seeded {added} scenarios from MACRO_SCENARIOS")


def _row_to_dict(row) -> dict:
    """Convert a ScenarioState DB row to the dict format used by existing code."""
    return {
        "scenario_id": row.scenario_id,
        "name": row.name,
        "description": row.description,
        "severity": row.severity,
        "lifecycle_state": row.lifecycle_state,
        "origin": row.origin,
        "trigger_keywords": json.loads(row.trigger_keywords_json or "[]"),
        "resolution_keywords": json.loads(row.resolution_keywords_json or "[]"),
        "stocks_to_avoid": json.loads(row.stocks_to_avoid_json or "[]"),
        "potential_beneficiaries": json.loads(row.potential_beneficiaries_json or "[]"),
        "sectors_at_risk": json.loads(row.sectors_at_risk_json or "[]"),
        "first_detected_at": row.first_detected_at,
        "last_evidence_at": row.last_evidence_at,
        "evidence_count": row.evidence_count,
        "resolution_evidence_count": row.resolution_evidence_count,
        "consecutive_misses": row.consecutive_misses,
        "resolved_at": row.resolved_at,
        "resolution_reason": row.resolution_reason,
    }


def get_active_scenarios(db: Session) -> List[dict]:
    """Return all ACTIVE or DECLINING scenarios from DB in standard dict format."""
    from database import ScenarioState
    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(["ACTIVE", "DECLINING"]))
        .all()
    )
    return [_row_to_dict(r) for r in rows]


def get_all_scenarios(db: Session) -> List[dict]:
    """Return all scenarios (any state) for dashboard display."""
    from database import ScenarioState
    rows = db.query(ScenarioState).order_by(ScenarioState.first_detected_at.desc()).all()
    result = []
    for r in rows:
        d = _row_to_dict(r)
        d["last_ai_review_at"] = r.last_ai_review_at.isoformat() if r.last_ai_review_at else None
        d["ai_review_summary"] = r.ai_review_summary
        result.append(d)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Deterministic Keyword Scanning
# ═══════════════════════════════════════════════════════════════════════════

def _extract_titles(news_items: list) -> List[str]:
    """Extract lowercased titles from news items (various formats)."""
    titles = []
    for item in news_items:
        if isinstance(item, dict):
            t = item.get("title") or item.get("headline") or ""
        elif isinstance(item, str):
            t = item
        else:
            continue
        if t:
            titles.append(t.lower().strip())
    return titles


def scan_trigger_keywords(db: Session, news_items: list) -> List[dict]:
    """
    Layer 1a: Update trigger evidence for all active scenarios.
    Returns list of active scenarios with matched evidence (same format as
    detect_active_macro_scenarios).
    """
    from database import ScenarioState

    titles = _extract_titles(news_items)
    if not titles:
        return []

    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(["ACTIVE", "DECLINING"]))
        .all()
    )

    now = datetime.utcnow()
    active_with_evidence = []

    for row in rows:
        keywords = json.loads(row.trigger_keywords_json or "[]")
        evidence = []
        for title in titles:
            hits = [kw for kw in keywords if kw in title]
            if hits:
                evidence.append({"title": title, "keywords": hits})

        if evidence:
            row.last_evidence_at = now
            row.evidence_count = (row.evidence_count or 0) + len(evidence)
            row.consecutive_misses = 0

            # Re-activate DECLINING scenario if cooldown passed
            if row.lifecycle_state == "DECLINING":
                state_changed = row.state_changed_at or now - timedelta(hours=1)
                if (now - state_changed).total_seconds() >= REACTIVATION_COOLDOWN_SECONDS:
                    row.lifecycle_state = "ACTIVE"
                    row.state_changed_at = now
                    logger.info(
                        f"[ScenarioLifecycle] Re-activated '{row.scenario_id}' "
                        f"(new trigger evidence after cooldown)"
                    )

            active_with_evidence.append({
                "scenario_id": row.scenario_id,
                "name": row.name,
                "severity": row.severity,
                "description": row.description,
                "evidence": evidence[:3],
                "stocks_to_avoid": json.loads(row.stocks_to_avoid_json or "[]"),
                "potential_beneficiaries": json.loads(row.potential_beneficiaries_json or "[]"),
            })
        else:
            row.consecutive_misses = (row.consecutive_misses or 0) + 1

    db.commit()
    return active_with_evidence


def scan_resolution_keywords(db: Session, news_items: list) -> List[dict]:
    """
    Layer 1b: Detect scenario resolution via deterministic keywords.
    Returns list of {scenario_id, action, evidence} for logging.
    """
    from database import ScenarioState

    titles = _extract_titles(news_items)
    if not titles:
        return []

    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(["ACTIVE", "DECLINING"]))
        .all()
    )

    now = datetime.utcnow()
    resolution_events = []

    for row in rows:
        res_keywords = json.loads(row.resolution_keywords_json or "[]")
        if not res_keywords:
            continue

        matched_titles = []
        for title in titles:
            hits = [kw for kw in res_keywords if kw in title]
            if hits:
                matched_titles.append(title)

        if not matched_titles:
            continue

        row.resolution_evidence_count = (row.resolution_evidence_count or 0) + len(matched_titles)
        evidence_summary = "; ".join(matched_titles[:3])

        if row.resolution_evidence_count >= RESOLUTION_RESOLVED_THRESHOLD:
            row.lifecycle_state = "RESOLVED"
            row.resolved_at = now
            row.state_changed_at = now
            row.resolution_reason = f"Resolution keywords matched {row.resolution_evidence_count}x: {evidence_summary}"
            action = "RESOLVED"
            logger.warning(
                f"[ScenarioLifecycle] RESOLVED '{row.scenario_id}': {evidence_summary}"
            )
        elif row.resolution_evidence_count >= RESOLUTION_DECLINING_THRESHOLD:
            if row.lifecycle_state != "DECLINING":
                row.lifecycle_state = "DECLINING"
                row.state_changed_at = now
                # Drop severity by one level
                old_sev = row.severity
                idx = SEVERITY_ORDER.index(old_sev) if old_sev in SEVERITY_ORDER else 2
                new_idx = max(0, idx - 1)
                row.severity = SEVERITY_ORDER[new_idx]
                row.severity_changed_at = now
                action = "DECLINING"
                logger.warning(
                    f"[ScenarioLifecycle] DECLINING '{row.scenario_id}' "
                    f"(severity {old_sev} → {row.severity}): {evidence_summary}"
                )
            else:
                action = "DECLINING_CONTINUED"
        else:
            action = "RESOLUTION_EVIDENCE_ACCUMULATING"

        resolution_events.append({
            "scenario_id": row.scenario_id,
            "action": action,
            "evidence": matched_titles[:3],
            "resolution_evidence_count": row.resolution_evidence_count,
        })

    if resolution_events:
        db.commit()

    return resolution_events


# ═══════════════════════════════════════════════════════════════════════════
# Time-based Decay
# ═══════════════════════════════════════════════════════════════════════════

def decay_stale_scenarios(db: Session):
    """
    Auto-demote scenarios that have gone silent (no trigger keyword matches).
    Seed scenarios are never deleted, only expired.
    """
    from database import ScenarioState

    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(["ACTIVE", "DECLINING"]))
        .all()
    )

    now = datetime.utcnow()
    changed = False

    for row in rows:
        misses = row.consecutive_misses or 0

        if row.lifecycle_state == "ACTIVE" and misses >= DECAY_DECLINING_MISSES:
            row.lifecycle_state = "DECLINING"
            row.state_changed_at = now
            changed = True
            logger.info(
                f"[ScenarioLifecycle] Decay: '{row.scenario_id}' → DECLINING "
                f"({misses} consecutive misses)"
            )
        elif row.lifecycle_state == "ACTIVE" and misses >= DECAY_SEVERITY_DROP_MISSES:
            old_sev = row.severity
            idx = SEVERITY_ORDER.index(old_sev) if old_sev in SEVERITY_ORDER else 2
            new_idx = max(0, idx - 1)
            if SEVERITY_ORDER[new_idx] != old_sev:
                row.severity = SEVERITY_ORDER[new_idx]
                row.severity_changed_at = now
                changed = True
                logger.info(
                    f"[ScenarioLifecycle] Decay: '{row.scenario_id}' severity "
                    f"{old_sev} → {row.severity} ({misses} consecutive misses)"
                )
        elif row.lifecycle_state == "DECLINING" and misses >= DECAY_EXPIRED_MISSES:
            row.lifecycle_state = "EXPIRED"
            row.resolved_at = now
            row.state_changed_at = now
            row.resolution_reason = f"Expired after {misses} scans with no evidence"
            changed = True
            logger.info(
                f"[ScenarioLifecycle] Decay: '{row.scenario_id}' → EXPIRED "
                f"({misses} consecutive misses)"
            )

    if changed:
        db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: AI-Powered Lifecycle Review
# ═══════════════════════════════════════════════════════════════════════════

_AI_REVIEW_PROMPT = """\
You are a geopolitical and macroeconomic scenario analyst for an autonomous stock trading system.
Your job is to assess whether tracked macro scenarios are still relevant based on the latest news.

## Current Tracked Scenarios
{scenarios_block}

## Recent News Headlines (last 6 hours)
{news_block}

## Your Tasks
1. For each ACTIVE/DECLINING scenario, assess whether evidence supports its current state.
   Recommend: MAINTAIN, ESCALATE (increase severity), DE_ESCALATE (decrease severity), or RESOLVE (scenario is over).
2. Identify if any news headlines suggest an entirely NEW macro scenario not currently tracked.
   Only propose scenarios with CLEAR market implications and at least 2 supporting headlines.
3. For any proposed new scenario, specify: name, description, trigger_keywords (at least 5),
   resolution_keywords (at least 3), stocks_to_avoid, potential_beneficiaries, initial severity.

Respond ONLY with valid JSON (no markdown, no explanation outside JSON):
{{
  "assessments": [
    {{
      "scenario_id": "...",
      "recommendation": "MAINTAIN" | "ESCALATE" | "DE_ESCALATE" | "RESOLVE",
      "new_severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | null,
      "reasoning": "1-2 sentences"
    }}
  ],
  "new_scenarios": [
    {{
      "name": "...",
      "description": "2-3 sentence description of the scenario and its market impact",
      "trigger_keywords": ["kw1", "kw2", "kw3", "kw4", "kw5"],
      "resolution_keywords": ["kw1", "kw2", "kw3"],
      "stocks_to_avoid": ["SYM1"],
      "potential_beneficiaries": ["SYM1", "SYM2"],
      "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",
      "evidence_headlines": ["exact headline 1", "exact headline 2"]
    }}
  ]
}}
"""


def _call_ai(ai_provider: str, api_key: str, prompt: str) -> str:
    """Call AI (Ollama or DeepSeek) with a single user message."""
    messages = [{"role": "user", "content": prompt}]

    if ai_provider == "ollama":
        from deepseek_ai import _call_ollama
        return _call_ollama(messages, temperature=0.1)
    else:
        from deepseek_ai import _call_deepseek_api
        return _call_deepseek_api(api_key, messages, max_tokens=3000, temperature=0.1)


def _parse_ai_json(content: str) -> Optional[dict]:
    """Parse JSON from AI response with fallback regex extraction."""
    # Strip think tags from reasoning models
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    # Strip markdown code fences
    content = re.sub(r"```(?:json)?\s*", "", content).strip()
    content = re.sub(r"```\s*$", "", content).strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try regex extraction
        match = re.search(r'(\{.*\})', content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return None


def _get_known_tickers() -> set:
    """Build a set of known valid ticker symbols for anti-hallucination validation."""
    known = set()
    try:
        from news_intelligence import SYMBOL_SECTOR_MAP
        known.update(SYMBOL_SECTOR_MAP.keys())
    except Exception:
        pass
    try:
        from market_data import GLOBAL_POPULAR_STOCKS
        for stocks in GLOBAL_POPULAR_STOCKS.values():
            known.update(stocks)
    except Exception:
        pass
    # Add common ETFs / tickers that might not be in the maps
    known.update([
        "SPY", "QQQ", "TQQQ", "SOXL", "GLD", "IAU", "SLV", "GDX",
        "USO", "UCO", "BNO", "XOM", "CVX", "OXY", "VIX", "TLT",
        "IBIT", "META", "AAPL", "MSFT", "AMZN", "GOOGL", "NVDA",
        "TSLA", "TSM", "AVGO", "AMD", "ASML", "INTC", "LMT", "RTX",
        "NOC", "GD", "JPM", "V", "MA", "BIDU", "BABA",
    ])
    return known


def ai_lifecycle_review(
    db: Session,
    ai_provider: str,
    api_key: str,
    recent_news: list,
) -> Optional[dict]:
    """
    Layer 2: AI-powered periodic review of all scenarios.
    Returns validated review dict or None on failure.
    """
    from database import ScenarioState

    # Gather all non-expired scenarios (or recently expired)
    cutoff = datetime.utcnow() - timedelta(days=7)
    rows = (
        db.query(ScenarioState)
        .filter(
            (ScenarioState.lifecycle_state.in_(["ACTIVE", "DECLINING"]))
            | (
                (ScenarioState.lifecycle_state.in_(["RESOLVED", "EXPIRED"]))
                & (ScenarioState.resolved_at > cutoff)
            )
        )
        .all()
    )

    if not rows:
        return None

    now = datetime.utcnow()

    # Build scenarios block
    scenario_lines = []
    for r in rows:
        days_active = (now - r.first_detected_at).days if r.first_detected_at else 0
        last_ev_ago = "never"
        if r.last_evidence_at:
            hours_ago = (now - r.last_evidence_at).total_seconds() / 3600
            last_ev_ago = f"{hours_ago:.1f}h ago"
        scenario_lines.append(
            f"- [{r.lifecycle_state}] {r.scenario_id}: \"{r.name}\" "
            f"(severity={r.severity}, {days_active}d active, "
            f"evidence={r.evidence_count}, last_evidence={last_ev_ago}, "
            f"resolution_hits={r.resolution_evidence_count})"
        )
    scenarios_block = "\n".join(scenario_lines)

    # Build news block (last 50 headlines)
    titles = _extract_titles(recent_news)[:50]
    news_block = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    if not news_block:
        news_block = "(No recent headlines available)"

    prompt = _AI_REVIEW_PROMPT.format(
        scenarios_block=scenarios_block,
        news_block=news_block,
    )

    try:
        raw_response = _call_ai(ai_provider, api_key, prompt)
        review = _parse_ai_json(raw_response)
        if not review:
            logger.warning("[ScenarioLifecycle] AI review returned unparseable response")
            return None
    except Exception as e:
        logger.error(f"[ScenarioLifecycle] AI review call failed: {e}")
        return None

    # ── Validate assessments ──
    valid_assessments = []
    existing_ids = {r.scenario_id for r in rows}
    for a in review.get("assessments", []):
        sid = a.get("scenario_id", "")
        rec = a.get("recommendation", "").upper()
        if sid not in existing_ids:
            continue
        if rec not in ("MAINTAIN", "ESCALATE", "DE_ESCALATE", "RESOLVE"):
            continue
        new_sev = a.get("new_severity")
        if new_sev and new_sev not in SEVERITY_ORDER:
            new_sev = None
        valid_assessments.append({
            "scenario_id": sid,
            "recommendation": rec,
            "new_severity": new_sev,
            "reasoning": a.get("reasoning", ""),
        })
    review["assessments"] = valid_assessments

    # ── Validate new scenarios ──
    valid_new = []
    known_tickers = _get_known_tickers()
    title_set = set(titles)  # for evidence verification

    for ns in review.get("new_scenarios", []):
        name = ns.get("name", "")
        if not name:
            continue
        triggers = ns.get("trigger_keywords", [])
        if len(triggers) < 3:  # relaxed from 5 to 3 for practical use
            continue
        # Verify evidence headlines exist in actual news
        evidence = ns.get("evidence_headlines", [])
        verified = [e for e in evidence if e.lower().strip() in title_set]
        if len(verified) < 1:
            logger.info(
                f"[ScenarioLifecycle] Rejected AI scenario '{name}': "
                f"evidence headlines not found in actual news"
            )
            continue
        # Filter tickers to known set
        beneficiaries = [t for t in ns.get("potential_beneficiaries", []) if t in known_tickers]
        avoid = [t for t in ns.get("stocks_to_avoid", []) if t in known_tickers]
        if not beneficiaries and not avoid:
            logger.info(
                f"[ScenarioLifecycle] Rejected AI scenario '{name}': "
                f"no valid tickers after filtering"
            )
            continue
        # Check for duplicate (keyword overlap with existing)
        existing_keywords = set()
        for r in rows:
            existing_keywords.update(json.loads(r.trigger_keywords_json or "[]"))
        overlap = len(set(triggers) & existing_keywords)
        if overlap > len(triggers) * 0.5:
            logger.info(
                f"[ScenarioLifecycle] Rejected AI scenario '{name}': "
                f"too much keyword overlap with existing scenarios"
            )
            continue

        severity = ns.get("severity", "MEDIUM")
        if severity not in SEVERITY_ORDER:
            severity = "MEDIUM"

        valid_new.append({
            "name": name,
            "description": ns.get("description", ""),
            "trigger_keywords": triggers,
            "resolution_keywords": ns.get("resolution_keywords", []),
            "stocks_to_avoid": avoid,
            "potential_beneficiaries": beneficiaries,
            "severity": severity,
            "evidence_headlines": verified,
        })
    review["new_scenarios"] = valid_new

    return review


def apply_ai_review(db: Session, review: dict):
    """Apply validated AI review results to the database."""
    from database import ScenarioState

    now = datetime.utcnow()

    # ── Apply assessments ──
    for a in review.get("assessments", []):
        row = db.query(ScenarioState).filter(
            ScenarioState.scenario_id == a["scenario_id"]
        ).first()
        if not row:
            continue

        rec = a["recommendation"]
        reasoning = a.get("reasoning", "")

        if rec == "RESOLVE":
            row.lifecycle_state = "RESOLVED"
            row.resolved_at = now
            row.state_changed_at = now
            row.resolution_reason = f"AI review: {reasoning}"
            logger.warning(
                f"[ScenarioLifecycle] AI RESOLVED '{row.scenario_id}': {reasoning}"
            )

        elif rec == "ESCALATE":
            old_sev = row.severity
            new_sev = a.get("new_severity")
            if new_sev and new_sev in SEVERITY_ORDER:
                row.severity = new_sev
            else:
                idx = SEVERITY_ORDER.index(old_sev) if old_sev in SEVERITY_ORDER else 2
                row.severity = SEVERITY_ORDER[min(len(SEVERITY_ORDER) - 1, idx + 1)]
            if row.lifecycle_state == "DECLINING":
                row.lifecycle_state = "ACTIVE"
            row.severity_changed_at = now
            row.state_changed_at = now
            logger.info(
                f"[ScenarioLifecycle] AI ESCALATED '{row.scenario_id}': "
                f"{old_sev} → {row.severity}. {reasoning}"
            )

        elif rec == "DE_ESCALATE":
            old_sev = row.severity
            new_sev = a.get("new_severity")
            if new_sev and new_sev in SEVERITY_ORDER:
                row.severity = new_sev
            else:
                idx = SEVERITY_ORDER.index(old_sev) if old_sev in SEVERITY_ORDER else 2
                row.severity = SEVERITY_ORDER[max(0, idx - 1)]
            row.severity_changed_at = now
            if row.lifecycle_state == "ACTIVE":
                row.lifecycle_state = "DECLINING"
                row.state_changed_at = now
            logger.info(
                f"[ScenarioLifecycle] AI DE_ESCALATED '{row.scenario_id}': "
                f"{old_sev} → {row.severity}. {reasoning}"
            )

        # MAINTAIN — no changes needed

        row.last_ai_review_at = now
        row.ai_review_summary = reasoning

    # ── Create new AI-generated scenarios ──
    for ns in review.get("new_scenarios", []):
        slug = re.sub(r'[^a-z0-9]+', '_', ns["name"].lower())[:30].strip('_')
        scenario_id = f"ai_gen_{now.strftime('%Y%m%d')}_{slug}"

        # Check if already exists
        existing = db.query(ScenarioState).filter(
            ScenarioState.scenario_id == scenario_id
        ).first()
        if existing:
            continue

        row = ScenarioState(
            scenario_id=scenario_id,
            name=ns["name"],
            description=ns.get("description", ""),
            severity=ns.get("severity", "MEDIUM"),
            lifecycle_state="ACTIVE",
            origin="ai_generated",
            trigger_keywords_json=json.dumps(ns.get("trigger_keywords", [])),
            resolution_keywords_json=json.dumps(ns.get("resolution_keywords", [])),
            stocks_to_avoid_json=json.dumps(ns.get("stocks_to_avoid", [])),
            potential_beneficiaries_json=json.dumps(ns.get("potential_beneficiaries", [])),
            sectors_at_risk_json=json.dumps([]),
            first_detected_at=now,
            last_evidence_at=now,
            evidence_count=len(ns.get("evidence_headlines", [])),
            last_ai_review_at=now,
            ai_review_summary=f"Created from news: {'; '.join(ns.get('evidence_headlines', [])[:2])}",
        )
        db.add(row)
        logger.warning(
            f"[ScenarioLifecycle] AI created new scenario '{scenario_id}': "
            f"{ns['name']} (severity={ns.get('severity', 'MEDIUM')})"
        )

    db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: Combined lifecycle scan (called from main.py)
# ═══════════════════════════════════════════════════════════════════════════

# Track last AI review time at module level
_last_ai_review_ts: float = 0.0
AI_REVIEW_INTERVAL = 1800  # 30 minutes


def run_lifecycle_scan(
    db: Session,
    news_items: list,
    ai_provider: str = "",
    api_key: str = "",
) -> Tuple[List[dict], List[dict]]:
    """
    Run the full lifecycle scan (called every 10 min from background_news_scan).

    Returns:
        (active_scenarios, resolution_events)
    """
    global _last_ai_review_ts

    # Layer 1a: Update trigger evidence, get active scenarios
    active_scenarios = scan_trigger_keywords(db, news_items)

    # Layer 1b: Check for resolutions
    resolution_events = scan_resolution_keywords(db, news_items)

    # Time-based decay
    decay_stale_scenarios(db)

    # Layer 2: AI review (every 30 minutes)
    now = time.time()
    if ai_provider and (now - _last_ai_review_ts) > AI_REVIEW_INTERVAL:
        try:
            review = ai_lifecycle_review(db, ai_provider, api_key, news_items)
            if review:
                apply_ai_review(db, review)
                n_assess = len(review.get("assessments", []))
                n_new = len(review.get("new_scenarios", []))
                logger.info(
                    f"[ScenarioLifecycle] AI review complete: "
                    f"{n_assess} assessments, {n_new} new scenarios"
                )
        except Exception as e:
            logger.error(f"[ScenarioLifecycle] AI review failed: {e}")
        _last_ai_review_ts = now

    return active_scenarios, resolution_events
