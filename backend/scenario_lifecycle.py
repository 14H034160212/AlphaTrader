"""
Scenario Lifecycle Manager

Dynamic, DB-backed lifecycle system that can:
  1. Detect scenario resolution via deterministic keywords (Layer 1)
  2. Periodically review scenarios via AI (Layer 2)
  3. Store lifecycle state in DB (Layer 3)
  4. Auto-generate new scenarios from news (Layer 4)
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

class State:
    ACTIVE = "ACTIVE"
    DECLINING = "DECLINING"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"

ACTIVE_STATES = [State.ACTIVE, State.DECLINING]

class Severity:
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    BULLISH = "BULLISH"

SEVERITY_ORDER = [Severity.BULLISH, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]

# Thresholds
RESOLUTION_DECLINING_THRESHOLD = 2
RESOLUTION_RESOLVED_THRESHOLD = 4
DECAY_SEVERITY_DROP_MISSES = 18   # ~3 hours
DECAY_DECLINING_MISSES = 36       # ~6 hours
DECAY_EXPIRED_MISSES = 72         # ~12 hours
REACTIVATION_COOLDOWN_SECONDS = 1200  # 20 min


# ── Helpers ──────────────────────────────────────────────────────────────────

def _jload(val) -> list:
    """Parse JSON list from a nullable DB column."""
    return json.loads(val or "[]")


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


def _severity_index(sev: str) -> int:
    return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 2


def _drop_severity(current: str) -> str:
    idx = _severity_index(current)
    return SEVERITY_ORDER[max(0, idx - 1)]


def _raise_severity(current: str) -> str:
    idx = _severity_index(current)
    return SEVERITY_ORDER[min(len(SEVERITY_ORDER) - 1, idx + 1)]


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: DB Seed & Read
# ═══════════════════════════════════════════════════════════════════════════

def seed_scenarios_from_hardcoded(db: Session):
    """
    Populate scenario_states table from MACRO_SCENARIOS on first boot.
    Only inserts scenarios that don't already exist.
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
            severity=data.get("severity", Severity.MEDIUM),
            lifecycle_state=State.ACTIVE,
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
        "trigger_keywords": _jload(row.trigger_keywords_json),
        "resolution_keywords": _jload(row.resolution_keywords_json),
        "stocks_to_avoid": _jload(row.stocks_to_avoid_json),
        "potential_beneficiaries": _jload(row.potential_beneficiaries_json),
        "sectors_at_risk": _jload(row.sectors_at_risk_json),
        "first_detected_at": row.first_detected_at,
        "last_evidence_at": row.last_evidence_at,
        "evidence_count": row.evidence_count,
        "resolution_evidence_count": row.resolution_evidence_count,
        "consecutive_misses": row.consecutive_misses,
        "resolved_at": row.resolved_at,
        "resolution_reason": row.resolution_reason,
    }


def get_active_scenarios(db: Session) -> List[dict]:
    """Return all ACTIVE or DECLINING scenarios from DB."""
    from database import ScenarioState
    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(ACTIVE_STATES))
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
# Layer 1: Deterministic Keyword Scanning (single-pass)
# ═══════════════════════════════════════════════════════════════════════════

def _scan_keywords_single_pass(rows, titles: List[str], now: datetime):
    """
    Combined trigger + resolution keyword scan in ONE pass over rows.
    Mutates rows in-place. Returns (active_with_evidence, resolution_events).
    """
    active_with_evidence = []
    resolution_events = []

    for row in rows:
        trigger_kws = _jload(row.trigger_keywords_json)
        res_kws = _jload(row.resolution_keywords_json)

        # Scan trigger keywords
        trigger_evidence = []
        for title in titles:
            hits = [kw for kw in trigger_kws if kw in title]
            if hits:
                trigger_evidence.append({"title": title, "keywords": hits})

        # Scan resolution keywords
        res_matched_titles = []
        if res_kws:
            for title in titles:
                hits = [kw for kw in res_kws if kw in title]
                if hits:
                    res_matched_titles.append(title)

        # --- Apply trigger evidence ---
        if trigger_evidence:
            row.last_evidence_at = now
            row.evidence_count = (row.evidence_count or 0) + len(trigger_evidence)
            row.consecutive_misses = 0

            if row.lifecycle_state == State.DECLINING:
                state_changed = row.state_changed_at or now - timedelta(hours=1)
                if (now - state_changed).total_seconds() >= REACTIVATION_COOLDOWN_SECONDS:
                    row.lifecycle_state = State.ACTIVE
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
                "evidence": trigger_evidence[:3],
                "stocks_to_avoid": _jload(row.stocks_to_avoid_json),
                "potential_beneficiaries": _jload(row.potential_beneficiaries_json),
            })
        else:
            row.consecutive_misses = (row.consecutive_misses or 0) + 1

        # --- Apply resolution evidence ---
        if res_matched_titles:
            row.resolution_evidence_count = (row.resolution_evidence_count or 0) + len(res_matched_titles)
            evidence_summary = "; ".join(res_matched_titles[:3])

            if row.resolution_evidence_count >= RESOLUTION_RESOLVED_THRESHOLD:
                row.lifecycle_state = State.RESOLVED
                row.resolved_at = now
                row.state_changed_at = now
                row.resolution_reason = f"Resolution keywords matched {row.resolution_evidence_count}x: {evidence_summary}"
                action = "RESOLVED"
                logger.warning(f"[ScenarioLifecycle] RESOLVED '{row.scenario_id}': {evidence_summary}")
            elif row.resolution_evidence_count >= RESOLUTION_DECLINING_THRESHOLD:
                if row.lifecycle_state != State.DECLINING:
                    row.lifecycle_state = State.DECLINING
                    row.state_changed_at = now
                    old_sev = row.severity
                    row.severity = _drop_severity(old_sev)
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
                "evidence": res_matched_titles[:3],
                "resolution_evidence_count": row.resolution_evidence_count,
            })

    return active_with_evidence, resolution_events


# ═══════════════════════════════════════════════════════════════════════════
# Time-based Decay (operates on pre-fetched rows)
# ═══════════════════════════════════════════════════════════════════════════

def _decay_stale_rows(rows, now: datetime) -> bool:
    """Auto-demote scenarios with no evidence. Returns True if any changed."""
    changed = False
    for row in rows:
        if row.lifecycle_state not in ACTIVE_STATES:
            continue
        misses = row.consecutive_misses or 0

        if row.lifecycle_state == State.ACTIVE and misses >= DECAY_DECLINING_MISSES:
            row.lifecycle_state = State.DECLINING
            row.state_changed_at = now
            changed = True
            logger.info(f"[ScenarioLifecycle] Decay: '{row.scenario_id}' → DECLINING ({misses} misses)")
        elif row.lifecycle_state == State.ACTIVE and misses >= DECAY_SEVERITY_DROP_MISSES:
            old_sev = row.severity
            new_sev = _drop_severity(old_sev)
            if new_sev != old_sev:
                row.severity = new_sev
                row.severity_changed_at = now
                changed = True
                logger.info(f"[ScenarioLifecycle] Decay: '{row.scenario_id}' severity {old_sev} → {new_sev}")
        elif row.lifecycle_state == State.DECLINING and misses >= DECAY_EXPIRED_MISSES:
            row.lifecycle_state = State.EXPIRED
            row.resolved_at = now
            row.state_changed_at = now
            row.resolution_reason = f"Expired after {misses} scans with no evidence"
            changed = True
            logger.info(f"[ScenarioLifecycle] Decay: '{row.scenario_id}' → EXPIRED ({misses} misses)")

    return changed


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
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    content = re.sub(r"```(?:json)?\s*", "", content).strip()
    content = re.sub(r"```\s*$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
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
    """Layer 2: AI-powered periodic review of all scenarios."""
    from database import ScenarioState

    cutoff = datetime.utcnow() - timedelta(days=7)
    rows = (
        db.query(ScenarioState)
        .filter(
            (ScenarioState.lifecycle_state.in_(ACTIVE_STATES))
            | (
                (ScenarioState.lifecycle_state.in_([State.RESOLVED, State.EXPIRED]))
                & (ScenarioState.resolved_at > cutoff)
            )
        )
        .all()
    )

    if not rows:
        return None

    now = datetime.utcnow()

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

    # Validate assessments
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

    # Validate new scenarios
    valid_new = []
    known_tickers = _get_known_tickers()
    title_set = set(titles)

    for ns in review.get("new_scenarios", []):
        name = ns.get("name", "")
        if not name:
            continue
        triggers = ns.get("trigger_keywords", [])
        if len(triggers) < 3:
            continue
        evidence = ns.get("evidence_headlines", [])
        verified = [e for e in evidence if e.lower().strip() in title_set]
        if len(verified) < 1:
            logger.info(f"[ScenarioLifecycle] Rejected AI scenario '{name}': evidence not in actual news")
            continue
        beneficiaries = [t for t in ns.get("potential_beneficiaries", []) if t in known_tickers]
        avoid = [t for t in ns.get("stocks_to_avoid", []) if t in known_tickers]
        if not beneficiaries and not avoid:
            logger.info(f"[ScenarioLifecycle] Rejected AI scenario '{name}': no valid tickers")
            continue
        existing_keywords = set()
        for r in rows:
            existing_keywords.update(_jload(r.trigger_keywords_json))
        overlap = len(set(triggers) & existing_keywords)
        if overlap > len(triggers) * 0.5:
            logger.info(f"[ScenarioLifecycle] Rejected AI scenario '{name}': keyword overlap")
            continue

        severity = ns.get("severity", Severity.MEDIUM)
        if severity not in SEVERITY_ORDER:
            severity = Severity.MEDIUM

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

    for a in review.get("assessments", []):
        row = db.query(ScenarioState).filter(
            ScenarioState.scenario_id == a["scenario_id"]
        ).first()
        if not row:
            continue

        rec = a["recommendation"]
        reasoning = a.get("reasoning", "")

        if rec == "RESOLVE":
            row.lifecycle_state = State.RESOLVED
            row.resolved_at = now
            row.state_changed_at = now
            row.resolution_reason = f"AI review: {reasoning}"
            logger.warning(f"[ScenarioLifecycle] AI RESOLVED '{row.scenario_id}': {reasoning}")

        elif rec == "ESCALATE":
            old_sev = row.severity
            new_sev = a.get("new_severity")
            row.severity = new_sev if (new_sev and new_sev in SEVERITY_ORDER) else _raise_severity(old_sev)
            if row.lifecycle_state == State.DECLINING:
                row.lifecycle_state = State.ACTIVE
            row.severity_changed_at = now
            row.state_changed_at = now
            logger.info(f"[ScenarioLifecycle] AI ESCALATED '{row.scenario_id}': {old_sev} → {row.severity}")

        elif rec == "DE_ESCALATE":
            old_sev = row.severity
            new_sev = a.get("new_severity")
            row.severity = new_sev if (new_sev and new_sev in SEVERITY_ORDER) else _drop_severity(old_sev)
            row.severity_changed_at = now
            if row.lifecycle_state == State.ACTIVE:
                row.lifecycle_state = State.DECLINING
                row.state_changed_at = now
            logger.info(f"[ScenarioLifecycle] AI DE_ESCALATED '{row.scenario_id}': {old_sev} → {row.severity}")

        row.last_ai_review_at = now
        row.ai_review_summary = reasoning

    # Create new AI-generated scenarios
    for ns in review.get("new_scenarios", []):
        slug = re.sub(r'[^a-z0-9]+', '_', ns["name"].lower())[:30].strip('_')
        scenario_id = f"ai_gen_{now.strftime('%Y%m%d')}_{slug}"

        existing = db.query(ScenarioState).filter(
            ScenarioState.scenario_id == scenario_id
        ).first()
        if existing:
            continue

        row = ScenarioState(
            scenario_id=scenario_id,
            name=ns["name"],
            description=ns.get("description", ""),
            severity=ns.get("severity", Severity.MEDIUM),
            lifecycle_state=State.ACTIVE,
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
            f"{ns['name']} (severity={ns.get('severity', Severity.MEDIUM)})"
        )

    db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Combined lifecycle scan (called from main.py)
# ═══════════════════════════════════════════════════════════════════════════

_last_ai_review_ts: float = 0.0
AI_REVIEW_INTERVAL = 1800  # 30 minutes


def run_lifecycle_scan(
    db: Session,
    news_items: list,
    ai_provider: str = "",
    api_key: str = "",
) -> Tuple[List[dict], List[dict]]:
    """
    Run the full lifecycle scan (called every ~15 min from background_news_scan).
    Single DB query, single title extraction, single pass over rows.

    Returns: (active_scenarios, resolution_events)
    """
    global _last_ai_review_ts
    from database import ScenarioState

    # Single DB query for all active/declining scenarios
    rows = (
        db.query(ScenarioState)
        .filter(ScenarioState.lifecycle_state.in_(ACTIVE_STATES))
        .all()
    )

    # Single title extraction
    titles = _extract_titles(news_items)

    now = datetime.utcnow()

    # Layer 1: Combined trigger + resolution scan in one pass
    active_scenarios, resolution_events = _scan_keywords_single_pass(rows, titles, now)

    # Time-based decay (operates on same pre-fetched rows)
    _decay_stale_rows(rows, now)

    db.commit()

    # Layer 2: AI review (every 30 minutes)
    ts_now = time.time()
    if ai_provider and (ts_now - _last_ai_review_ts) > AI_REVIEW_INTERVAL:
        try:
            review = ai_lifecycle_review(db, ai_provider, api_key, news_items)
            if review:
                apply_ai_review(db, review)
                n_assess = len(review.get("assessments", []))
                n_new = len(review.get("new_scenarios", []))
                logger.info(f"[ScenarioLifecycle] AI review: {n_assess} assessments, {n_new} new scenarios")
        except Exception as e:
            logger.error(f"[ScenarioLifecycle] AI review failed: {e}")
        _last_ai_review_ts = ts_now

    return active_scenarios, resolution_events
