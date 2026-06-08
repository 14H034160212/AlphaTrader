"""Serenity supply-chain chokepoint lens — PRIMARY decision framework.

Distilled from the `serenity-aleabitoreddit` skill (5,857 tweets of trader
Serenity / @aleabitoreddit, an AI/semiconductor supply-chain analyst). Per the
user's 2026-06-08 directive, this lens LEADS stock selection: the brain reasons
through Serenity's chokepoint methodology FIRST, and the platform's prior
methods (technicals, DCF, RL feedback, the old large-cap-first prior) become
SUPPORTING evidence used to pressure-test the Serenity thesis — never to
override it.

Decision-support only. This module produces prompt text; it never trades,
places, or cancels orders. Serenity's self-reported returns are unverified and
carry survivorship/selection bias; his names are volatile micro/small-caps.

Data sources (read once, cached):
  .claude/skills/serenity-aleabitoreddit/data/ticker_stats.txt   — his universe
  .claude/skills/serenity-aleabitoreddit/references/methodology.md — the checklist
  .claude/skills/serenity-aleabitoreddit/references/track-record.md — dated calls
"""
import os
import logging

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SKILL_DIR = os.path.join(_REPO_ROOT, ".claude", "skills", "serenity-aleabitoreddit")
_UNIVERSE_PATH = os.path.join(_SKILL_DIR, "data", "ticker_stats.txt")
_METHODOLOGY_PATH = os.path.join(_SKILL_DIR, "references", "methodology.md")
_TRACK_RECORD_PATH = os.path.join(_SKILL_DIR, "references", "track-record.md")

# Conviction tiers by total mention count in his feed (a rough proxy — he tweets
# most about his highest-conviction bottleneck names).
_TIER_CORE = 150       # flagship / repeatedly-pressed bottleneck thesis
_TIER_COVERED = 20     # a real, recurring name in his coverage
_TIER_MENTIONED = 2    # named at least a couple of times


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning("serenity_lens: could not read %s (%s)", path, e)
        return ""


def _load_universe():
    """Parse ticker_stats.txt → {TICKER: {'mentions': int, 'first': str, 'last': str}}."""
    out = {}
    text = _read(_UNIVERSE_PATH)
    for line in text.splitlines():
        parts = line.split()
        # rows look like: "AXTI        541   2025-12-22  2026-06-07"
        if len(parts) == 4 and parts[1].isdigit() and "-" in parts[2]:
            out[parts[0].upper()] = {
                "mentions": int(parts[1]),
                "first": parts[2],
                "last": parts[3],
            }
    return out


def _extract_checklist():
    """Pull the '## 15. The checklist' section verbatim from methodology.md."""
    text = _read(_METHODOLOGY_PATH)
    if not text:
        return ""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 15."):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


# Cache the static pieces at import time.
_UNIVERSE = _load_universe()
_CHECKLIST = _extract_checklist()


def _ticker_track_record(symbol, limit=6):
    """Grep track-record.md for dated-call rows where the ticker column matches."""
    sym = (symbol or "").upper()
    if not sym:
        return []
    rows = []
    for line in _read(_TRACK_RECORD_PATH).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # table is: Date | Ticker | Call | Outcome
        if len(cells) >= 4 and cells[1].upper() == sym:
            rows.append((cells[0], cells[2], cells[3]))
    # most recent last in the file (chronological) → take the tail
    return rows[-limit:]


def get_ticker_stance(symbol):
    """Return a structured read of Serenity's stance on `symbol`.

    Keys: in_universe (bool), tier (str), mentions (int), first/last (str),
    calls (list of (date, call, outcome)).
    """
    sym = (symbol or "").upper()
    # Strip exchange suffix for HK/CN names (e.g. 2382.HK) — his universe is US.
    base = sym.split(".")[0]
    info = _UNIVERSE.get(sym) or _UNIVERSE.get(base)
    if not info:
        return {"in_universe": False, "tier": "NOT-COVERED", "mentions": 0,
                "first": None, "last": None, "calls": []}
    m = info["mentions"]
    tier = ("CORE" if m >= _TIER_CORE else
            "COVERED" if m >= _TIER_COVERED else
            "MENTIONED" if m >= _TIER_MENTIONED else "PASSING")
    return {
        "in_universe": True, "tier": tier, "mentions": m,
        "first": info["first"], "last": info["last"],
        "calls": _ticker_track_record(sym) or _ticker_track_record(base),
    }


def build_serenity_lens_block(symbol, sector="Other"):
    """Build the PRIMARY decision-framework section injected into analyze_stock.

    The block (a) states the chokepoint mental model as the lead lens, (b) gives
    Serenity's actual stance on this ticker if he covers it, and (c) hands the
    model his checklist to vet any name. Supporting platform signals
    (technicals/DCF/RL) appear later in the prompt as pressure-test inputs.
    """
    stance = get_ticker_stance(symbol)

    header = (
        "## 🧭 SERENITY SUPPLY-CHAIN CHOKEPOINT LENS — PRIMARY FRAMEWORK\n"
        "*(User directive 2026-06-08: reason through THIS lens FIRST. The "
        "technicals, DCF, RL feedback and prior 'large-cap-first' notes below are "
        "SUPPORTING evidence to pressure-test the chokepoint thesis — they do NOT "
        "override it. Decision-support only: never copy his trades; his returns "
        "are self-reported/unverified with survivorship bias.)*\n\n"
        "**The core mental model:** Do NOT default to the obvious 'shovel seller' "
        "(e.g. NVDA). Trace the supply chain UPSTREAM to the single chokepoint a "
        "hyperscaler will pay anything to keep flowing — optical/CPO, InP "
        "substrate/epiwafer, memory/HBM, AI power/grid, packaging/equipment. The "
        "further upstream and the smaller the market cap, the more mispriced the "
        "bottleneck tends to be relative to the trillions of AI capex flowing "
        "downstream. **Overlooked small/mid-cap upstream chokepoints in the "
        "user's focus themes (semis / GPU-downstream supply chain / physical AI / "
        "robotics) are explicitly IN-SCOPE and PREFERRED here — this overrides any "
        "generic 'avoid small-cap' prior for these themes.**\n"
    )

    # Serenity's actual stance on this specific name.
    if stance["in_universe"]:
        tier_label = {
            "CORE": "🔥 CORE conviction name (flagship bottleneck thesis he presses repeatedly)",
            "COVERED": "✅ COVERED — a recurring name in his coverage",
            "MENTIONED": "• MENTIONED a handful of times",
            "PASSING": "· passing reference only (low weight)",
        }.get(stance["tier"], stance["tier"])
        lines = [
            f"\n**Serenity's stance on {symbol}:** {tier_label} — "
            f"{stance['mentions']} mentions, {stance['first']} → {stance['last']}."
        ]
        if stance["calls"]:
            lines.append("His dated calls (self-reported outcomes — treat as calibration, not proof):")
            for date, call, outcome in stance["calls"]:
                lines.append(f"  - [{date}] {call} → {outcome}")
        else:
            lines.append("(No dated-call rows recorded for this ticker; weight on mention frequency only.)")
        stance_block = "\n".join(lines) + "\n"
    else:
        stance_block = (
            f"\n**Serenity's stance on {symbol}:** NOT in his covered universe. "
            "Do NOT fabricate a view for him. Instead, vet this name FRESH against "
            "his checklist below — the more boxes it ticks, the more it fits a true "
            "chokepoint; few boxes = likely not his kind of setup.\n"
        )

    checklist_block = ""
    if _CHECKLIST:
        checklist_block = (
            "\n### Serenity's checklist (apply to this name; more 'yes' = stronger chokepoint fit)\n"
            + _CHECKLIST + "\n"
        )

    decision_guidance = (
        "\n**How to let the lens lead the decision:**\n"
        "- If the name is a genuine upstream chokepoint in a focus theme with "
        "checklist confirmation and a dated catalyst → that is the strongest BUY "
        "case, even at small/mid cap. Size to conviction × research depth, not to hype.\n"
        "- If it's the obvious downstream 'shovel seller' (mega-cap) already "
        "crowded/frontrun → the lens is skeptical of NEW capital; HOLD if owned, "
        "don't chase. (Still honor the LONG-TERM HOLD mandate for quality names already owned.)\n"
        "- If it fails the chokepoint test and isn't in a focus theme → default HOLD.\n"
        "- ALWAYS confirm current price/fundamentals yourself; his theses decay and "
        "his small-caps are volatile (20%+ days, dilution/single-customer/binary risk).\n"
    )

    return header + stance_block + checklist_block + decision_guidance


def recommended_tickers(min_mentions=50):
    """Serenity's recurring conviction universe — his 'recommended' names.

    Returns tickers he mentions at least `min_mentions` times, highest-conviction
    first. Used to drive the watchlist so the engine analyses ONLY Serenity's
    names (user directive 2026-06-08) rather than self-discovering others.
    Note: this is his coverage universe; the per-stock Serenity lens still
    decides BUY vs HOLD (e.g. it stays skeptical of mega-cap 'shovel sellers'
    like NVDA even though he mentions them often).
    """
    return [t for t, info in sorted(_UNIVERSE.items(),
                                    key=lambda kv: -kv[1]["mentions"])
            if info["mentions"] >= min_mentions]


def universe_size():
    """Number of distinct tickers in Serenity's loaded universe (for diagnostics)."""
    return len(_UNIVERSE)
